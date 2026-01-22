import streamlit as st
import pandas as pd
import numpy as np
import requests
import math
import plotly.express as px
import plotly.graph_objects as go
import datetime
from geopy.geocoders import Nominatim
from streamlit_searchbox import st_searchbox

# --- CONFIGURATION DE LA PAGE ---
st.set_page_config(page_title="Décarbonation Bâtiment", layout="wide")

# Fonction pour réinitialiser l'état quand un paramètre change
def reset_simulation():
    if st.session_state.get("simulation_lancee", False):
        st.session_state.parametres_modifies = True
        st.toast("⚠️ Des paramètres ont été modifiés. Cliquez sur **Simuler** pour mettre à jour les résultats.", icon="⚠️")

st.title("Décarbonez votre site et réduisez vos couts : votre étude de faisabilité")

# --- LOGIQUE PVGIS & GEOLOCALISATION ---
def rechercher_adresses(searchterm: str, pays: str):
    if not searchterm or len(searchterm) < 2:
        return []
    try:
        adresses = []
        
        if pays == "France":
            # API France (Base Adresse Nationale)
            try:
                url_fr = "https://api-adresse.data.gouv.fr/search/"
                params_fr = {"q": searchterm, "limit": 10}
                res_fr = requests.get(url_fr, params=params_fr, timeout=2)
                if res_fr.status_code == 200:
                    for f in res_fr.json().get("features", []):
                        adresses.append(f["properties"]["label"])
            except:
                pass
        elif pays == "Suisse":
            # API Swisstopo (Search API) - Plus précis pour la Suisse
            try:
                url_ch = "https://api3.geo.admin.ch/rest/services/api/SearchServer"
                params_ch = {
                    "searchText": searchterm,
                    "type": "locations",
                    "origins": "address",
                    "limit": 15
                }
                res_ch = requests.get(url_ch, params=params_ch, timeout=3)
                if res_ch.status_code == 200:
                    for f in res_ch.json().get("results", []):
                        adresses.append(f["attrs"]["label"].replace("<b>", "").replace("</b>", ""))
            except:
                # Fallback sur Photon si Swisstopo échoue
                try:
                    url_ph = "https://photon.komoot.io/api/"
                    params_ph = {"q": searchterm, "limit": 10, "lang": "fr"}
                    res_ph = requests.get(url_ph, params=params_ph, timeout=3)
                    if res_ph.status_code == 200:
                        for f in res_ph.json().get("features", []):
                            p = f.get("properties", {})
                            if p.get("countrycode") == "CH":
                                full = f"{p.get('street', '')} {p.get('housenumber', '')}, {p.get('postcode', '')} {p.get('city', '')}, Suisse"
                                adresses.append(full.strip(", "))
                except:
                    pass
        else:
            # API Photon pour les autres pays
            try:
                url_ph = "https://photon.komoot.io/api/"
                params_ph = {"q": searchterm, "limit": 10, "lang": "fr"}
                res_ph = requests.get(url_ph, params=params_ph, timeout=3)
                if res_ph.status_code == 200:
                    for f in res_ph.json().get("features", []):
                        p = f.get("properties", {})
                        full = f"{p.get('street', '')} {p.get('housenumber', '')}, {p.get('postcode', '')} {p.get('city', '')}, {p.get('country', '')}"
                        adresses.append(full.strip(", "))
            except:
                pass
            
        return list(dict.fromkeys(adresses))
    except Exception:
        return []

@st.cache_data
def obtenir_lat_lon(adresse):
    if not adresse:
        return None, None
    try:
        # Pour obtenir les coordonnées finales, on utilise Nominatim qui est précis une fois l'adresse choisie
        geolocator = Nominatim(user_agent="decarbonation_tool_final_v2")
        location = geolocator.geocode(adresse, timeout=5)
        if location:
            return location.latitude, location.longitude
    except Exception:
        # En cas d'échec de Nominatim, on peut essayer Photon pour les coordonnées aussi
        try:
            url = "https://photon.komoot.io/api/"
            params = {"q": adresse, "limit": 1}
            res = requests.get(url, params=params, timeout=5).json()
            if res["features"]:
                lon, lat = res["features"][0]["geometry"]["coordinates"]
                return lat, lon
        except:
            pass
    return None, None

@st.cache_data
def appeler_pvgis(lat, lon, angle, aspect):
    """
    aspect: 0=Sud, 90=Ouest, -90=Est, 180=Nord
    """
    url = "https://re.jrc.ec.europa.eu/api/v5_2/PVcalc"
    params = {
        "lat": lat,
        "lon": lon,
        "peakpower": 1,
        "loss": 14,
        "angle": angle,
        "aspect": aspect,
        "outputformat": "json"
    }
    try:
        response = requests.get(url, params=params)
        if response.status_code == 200:
            data = response.json()
            return data['outputs']['totals']['fixed']['E_y'] # Production annuelle en kWh
    except Exception as e:
        st.error(f"Erreur API PVGIS : {e}")
    return None

@st.cache_data
def appeler_pvgis_mensuel(lat, lon, angle, aspect):
    """
    Récupère les données mensuelles moyennes de PVGIS.
    """
    url = "https://re.jrc.ec.europa.eu/api/v5_2/PVcalc"
    params = {
        "lat": lat,
        "lon": lon,
        "peakpower": 1,
        "loss": 14,
        "angle": angle,
        "aspect": aspect,
        "outputformat": "json"
    }
    try:
        response = requests.get(url, params=params)
        if response.status_code == 200:
            data = response.json()
            # On récupère la liste des productions mensuelles (E_m)
            # E_m est la production mensuelle moyenne en kWh pour 1kWc
            return [m['E_m'] for m in data['outputs']['monthly']['fixed']]
    except Exception as e:
        st.error(f"Erreur API PVGIS Mensuel : {e}")
    return None

@st.cache_data
def appeler_pvgis_horaire(lat, lon, angle, aspect):
    """
    Récupère les données horaires de PVGIS (profil type sur 8760h).
    """
    url = "https://re.jrc.ec.europa.eu/api/v5_2/seriescalc"
    params = {
        "lat": lat,
        "lon": lon,
        "peakpower": 1,
        "loss": 14,
        "angle": angle,
        "aspect": aspect,
        "outputformat": "json",
        "pvcalculation": 1,
        "startyear": 2020,
        "endyear": 2020
    }
    try:
        response = requests.get(url, params=params)
        if response.status_code == 200:
            data = response.json()
            # On extrait la puissance horaire (P) pour chaque heure
            # PVGIS renvoie parfois des années différentes, on normalise sur 8760 points
            series = data['outputs']['hourly']
            # On ne garde que les 8760 premiers points si année bissextile ou autre
            return [h['P'] / 1000 for h in series[:8760]] # Convertit W en kW pour 1kWc
    except Exception:
        pass
    return None

def generer_profil_synthetique(type_bat, conso_annuelle):
    """
    Génère une courbe de charge théorique sur 8760h basée sur le type de bâtiment.
    """
    profil = []
    for h in range(8760):
        heure = h % 24
        # Résidentiel : pics matin et soir
        if type_bat == "Résidentiel":
            if 7 <= heure <= 9: w = 1.5
            elif 18 <= heure <= 22: w = 2.2
            elif 0 <= heure <= 6: w = 0.4
            else: w = 1.0
        # Bureaux : pic en journée de semaine (simplifié ici à tous les jours)
        elif type_bat == "Tertiaire / Bureaux":
            if 8 <= heure <= 18: w = 2.5
            else: w = 0.3
        # Industriel : stable en journée, fond la nuit
        else:
            if 6 <= heure <= 22: w = 2.0
            else: w = 0.8
        profil.append(w)
    
    total_w = sum(profil)
    return [p * (conso_annuelle / total_w) for p in profil]

def get_aspect(orientation_nom):
    mapping = {
        "Sud": 0,
        "Sud-Est": -45,
        "Sud-Ouest": 45,
        "Est": -90,
        "Ouest": 90,
        "Nord-Est": -135,
        "Nord-Ouest": 135,
        "Nord": 180
    }
    return mapping.get(orientation_nom, 0)

# --- LOGIQUE PRIX DYNAMIQUES (ENTSO-E) ---
@st.cache_data(ttl=86400) # Cache 24h
def recuperer_prix_dynamiques(pays, annee=2024):
    """
    Récupère les prix "Day-Ahead" du marché de gros via l'API publique d'ENTSO-E (données historiques).
    Si l'API échoue ou n'est pas configurée, renvoie un profil type basé sur des données réelles 2024.
    """
    code_pays = "FR" if pays == "France" else "CH"
    
    # PROFIL TYPE (Fallback) - Moyenne 2024 (France/Suisse)
    # On simule un profil horaire réaliste (bas le matin/nuit, haut pics de 8h et 19h)
    profil_type = []
    
    # On utilise une seed pour la reproductibilité
    np.random.seed(42)
    
    for h in range(8760):
        heure = h % 24
        # Base de prix entre 0.05 et 0.15 €/kWh
        base = 0.08
        if 7 <= heure <= 10: base = 0.12 # Pic matin
        elif 18 <= heure <= 21: base = 0.15 # Pic soir
        elif 0 <= heure <= 5: base = 0.05 # Nuit
        
        # Ajout d'une saisonnalité simple
        mois = (h // (24 * 30)) % 12
        if mois in [11, 0, 1]: base *= 1.3 # Hiver plus cher
        elif mois in [5, 6, 7]: base *= 0.8 # Été moins cher
        
        # Ajout de volatilité aléatoire
        base += np.random.normal(0, 0.01)
        
        profil_type.append(max(0.01, base))
    
    return profil_type

# --- BARRE LATÉRALE (INPUTS) ---
st.sidebar.header("🏢 Informations du Bâtiment")

# 1. Adresse avec Auto-complétion et état
if "adresse_validee" not in st.session_state:
    st.session_state.adresse_validee = "1 rue du cimetiere 68730 blotzheim"

# CSS pour enlever le liseré rouge de Streamlit sur la searchbox
st.markdown("""
    <style>
    div[data-baseweb="input"] {
        border-color: transparent !important;
    }
    div[class*="st-"] {
        border-color: transparent !important;
    }
    </style>
    """, unsafe_allow_html=True)

# On récupère le pays sélectionné même si l'adresse est validée pour la devise
if "pays_selectionne" not in st.session_state:
    st.session_state.pays_selectionne = "France"

if not st.session_state.adresse_validee:
    st.sidebar.info("👋 Bienvenue ! Veuillez sélectionner votre pays puis saisir votre adresse pour lancer l'étude.")
    st.sidebar.write("🌍 **Étape 1 : Choisir le pays**")
    pays_selectionne = st.sidebar.selectbox("Sélectionnez votre pays", ["France", "Suisse"], label_visibility="collapsed", on_change=reset_simulation)
    st.session_state.pays_selectionne = pays_selectionne
    
    # Définition de la devise selon le pays
    devise = "€" if pays_selectionne == "France" else "CHF"
    
    st.sidebar.write("📍 **Étape 2 : Saisir l'adresse**")
    search_key = f"search_adresse_{st.session_state.get('search_version', 0)}"
    # Ajout explicite du paramètre pour forcer l'affichage dans la barre latérale
    with st.sidebar:
        adresse_selectionnee = st_searchbox(
            lambda t: rechercher_adresses(t, pays_selectionne),
            key=search_key,
            placeholder="Saisissez votre adresse...",
            clear_on_submit=True,
        )
    if adresse_selectionnee:
        st.session_state.adresse_validee = adresse_selectionnee
        st.session_state.simulation_lancee = False # Réinitialise pour forcer un nouveau calcul
        st.rerun()
else:
    st.sidebar.write("📍 **Adresse sélectionnée :**")
    st.sidebar.info(st.session_state.adresse_validee)
    if st.sidebar.button("❌ Changer d'adresse"):
        st.session_state.adresse_validee = None
        # On incrémente une version pour forcer Streamlit à recréer le widget searchbox
        st.session_state.search_version = st.session_state.get('search_version', 0) + 1
        st.rerun()

# Récupération de la devise même si l'adresse est validée
devise = "€" if st.session_state.pays_selectionne == "France" else "CHF"

adresse = st.session_state.adresse_validee

# 2. Type de toit et Matériaux
st.sidebar.write("🏠 **Étape 3 : Caractéristiques du bâtiment**")

st.sidebar.write("🏚️ **Toiture**")
col_type, col_mat = st.sidebar.columns(2)
with col_type:
    type_toit = st.selectbox("Type", ["Plat", "Incliné"], label_visibility="collapsed", on_change=reset_simulation)

if type_toit == "Plat":
    with col_mat:
        materiau = st.selectbox("Matériau", ["Bitumineux", "Gravier"], label_visibility="collapsed", on_change=reset_simulation)
    
    # Choix de la variante pour toit plat
    variante_plat = st.sidebar.radio("Variante d'installation", ["Sud (Optimisé rendement)", "Est-Ouest (Optimisé surface)"], index=1, horizontal=True, on_change=reset_simulation)
    
    inclinaison = 10
    if "Sud" in variante_plat:
        selection_orientations = ["Sud"]
    else:
        selection_orientations = ["Est", "Ouest"]
    
    # Pour toit plat, pas de méthode de mesure (toujours surface réelle projetée)
    surface_dispo = st.sidebar.number_input("Surface totale (m²)", min_value=1, value=2200, step=1, on_change=reset_simulation)
    st.sidebar.markdown(f'<div style="font-size: 0.8rem; color: #666; margin-top: -15px; margin-bottom: 10px;">👉 {surface_dispo:,.0f} m²</div>'.replace(",", " "), unsafe_allow_html=True)
    mode_mesure = "Surface réelle"
    
    # Stockage des pans pour le calcul
    donnees_pans = []
    if len(selection_orientations) == 1:
        donnees_pans.append({"orientation": "Sud", "inclinaison": 10, "surface": surface_dispo})
    else:
        # En Est-Ouest, on divise la surface en deux (50% Est, 50% Ouest)
        donnees_pans.append({"orientation": "Est", "inclinaison": 10, "surface": surface_dispo / 2})
        donnees_pans.append({"orientation": "Ouest", "inclinaison": 10, "surface": surface_dispo / 2})
else:
    with col_mat:
        materiau = st.selectbox("Matériau", ["Tuile", "Tôle", "Eternit"], label_visibility="collapsed", on_change=reset_simulation)
    
    st.sidebar.subheader("Configuration des pans")
    mode_orientation = st.sidebar.radio("Type d'orientation", ["Mono-orientation", "Multi-orientations"], horizontal=True, on_change=reset_simulation)
    
    orientations_possibles = ["Nord", "Nord-Est", "Est", "Sud-Est", "Sud", "Sud-Ouest", "Ouest", "Nord-Ouest"]
    
    donnees_pans = []
    
    if mode_orientation == "Mono-orientation":
        col1, col2 = st.sidebar.columns(2)
        with col1:
            orient = st.selectbox("Orientation", orientations_possibles, index=4, on_change=reset_simulation)
        with col2:
            incli = st.number_input("Inclinaison (°)", min_value=0, max_value=90, value=10, on_change=reset_simulation)
        
        # Méthode de mesure juste avant surface disponible
        mode_mesure = st.sidebar.radio(
            "Méthode de mesure des surfaces", 
            ["Vue aérienne", "Surface réelle"], 
            horizontal=True,
            on_change=reset_simulation,
            help="**Vue aérienne** : La surface est calculée comme une projection horizontale. L'outil appliquera un correctif trigonométrique selon l'inclinaison pour obtenir la surface réelle du toit."
        )
        surf = st.sidebar.number_input("Surface totale (m²)", min_value=1, value=300, on_change=reset_simulation)
        st.sidebar.markdown(f'<div style="font-size: 0.8rem; color: #666; margin-top: -15px; margin-bottom: 10px;">👉 {surf:,.0f} m²</div>'.replace(",", " "), unsafe_allow_html=True)
        donnees_pans.append({"orientation": orient, "inclinaison": incli, "surface": surf})
    else:
        # Méthode de mesure juste après type d'orientation
        mode_mesure = st.sidebar.radio(
            "Méthode de mesure des surfaces", 
            ["Vue aérienne", "Surface réelle"], 
            horizontal=True,
            on_change=reset_simulation,
            help="**Vue aérienne** : La surface est calculée comme une projection horizontale. L'outil appliquera un correctif trigonométrique selon l'inclinaison pour obtenir la surface réelle du toit."
        )
        
        couples_possibles = {
            "Nord / Sud": ["Nord", "Sud"],
            "Est / Ouest": ["Est", "Ouest"],
            "Nord-Est / Sud-Ouest": ["Nord-Est", "Sud-Ouest"],
            "Nord-Ouest / Sud-Est": ["Nord-Ouest", "Sud-Est"]
        }
        choix_couple = st.sidebar.selectbox("Choisissez le couple d'orientations (2 pans)", list(couples_possibles.keys()), on_change=reset_simulation)
        selection_multi = couples_possibles[choix_couple]
        
        # --- LOGIQUE DE SYNCHRONISATION DES SURFACES ---
        if "last_selection_multi" not in st.session_state or st.session_state.last_selection_multi != selection_multi:
            st.session_state.last_selection_multi = selection_multi
            # Reset des surfaces lors du changement de couple
            st.session_state.surf_totale_multi = 300
            for o in selection_multi:
                st.session_state[f"surf_{o}"] = 150

        def update_from_total():
            val_total = st.session_state.surf_totale_multi
            for o in selection_multi:
                st.session_state[f"surf_{o}"] = int(val_total / len(selection_multi))
            reset_simulation()

        def update_from_individual():
            val_sum = sum(st.session_state[f"surf_{o}"] for o in selection_multi)
            st.session_state.surf_totale_multi = int(val_sum)
            reset_simulation()

        # Saisie de la surface totale juste après le choix du couple
        surf_totale_multi = st.sidebar.number_input(
            "Surface totale des 2 pans (m²)", 
            min_value=1, 
            key="surf_totale_multi",
            on_change=update_from_total
        )
        st.sidebar.markdown(f'<div style="font-size: 0.8rem; color: #666; margin-top: -15px; margin-bottom: 10px;">👉 {surf_totale_multi:,.0f} m²</div>'.replace(",", " "), unsafe_allow_html=True)

        # Tableau compact pour Multi-orientations avec titres
        if selection_multi:
            # On utilise des colonnes un peu plus larges pour les titres complets
            h1, h2, h3 = st.sidebar.columns([1.5, 1.8, 1.7])
            h1.caption("**Orientation**")
            h2.caption("**Inclinaison (°)**")
            h3.caption("**Surface (m²)**")
            
            for o in selection_multi:
                # Utilisation de colonnes alignées
                c1, c2, c3 = st.sidebar.columns([1.5, 1.8, 1.7])
                with c1:
                    st.write(f"{o}")
                with c2:
                    incli = st.number_input(f"Incl. {o}", min_value=0, max_value=90, value=10, key=f"incli_{o}", label_visibility="collapsed", on_change=reset_simulation)
                with c3:
                    # On permet la modification individuelle de la surface par pan
                    surf_pan_individuelle = st.number_input(
                        f"Surf. {o}", 
                        min_value=0, 
                        value=int(st.session_state.get(f"surf_{o}", 0)),
                        key=f"surf_{o}", 
                        label_visibility="collapsed", 
                        format="%d",
                        on_change=update_from_individual
                    )
                    st.markdown(f'<div style="font-size: 0.7rem; color: #888; margin-top: -10px;">{surf_pan_individuelle:,.0f} m²</div>'.replace(",", " "), unsafe_allow_html=True)
                
                donnees_pans.append({"orientation": o, "inclinaison": incli, "surface": surf_pan_individuelle})

# --- ÉTAPE 4 : INTRODUCTION ÉLECTRIQUE ---
st.sidebar.write("🔌 **Introduction électrique**")
col_unit, col_val = st.sidebar.columns([1, 1])

if st.session_state.pays_selectionne == "France":
    with col_unit:
        unite_intro = st.selectbox(
            "Unité Introduction", 
            ["kVA", "Ampères"], 
            index=0,
            label_visibility="collapsed",
            on_change=reset_simulation,
            help="L'unité de puissance d'introduction (puissance physique) de votre bâtiment."
        )
    with col_val:
        if unite_intro == "kVA":
            intro_val = st.number_input(
                "Valeur Intro (kVA)", 
                min_value=1.0, 
                value=850.0, 
                step=1.0, 
                format="%.0f",
                label_visibility="collapsed",
                on_change=reset_simulation
            )
        else:
            intro_val = st.number_input(
                "Valeur Intro (A)", 
                min_value=1.0, 
                value=250.0, 
                step=1.0, 
                format="%.0f",
                label_visibility="collapsed",
                on_change=reset_simulation
            )
    
    # Nouvel input pour l'abonnement en France
    st.sidebar.write("📜 **Abonnement contractuel**")
    
    # Choix de l'offre en France
    offres_france = ["Tarif bleu particulier", "Tarif bleu pro", "Tarif jaune", "Tarif vert"]
    offre_france_selectionnee = st.sidebar.selectbox(
        "Offre tarifaire",
        offres_france,
        index=2,
        on_change=reset_simulation,
        help="Sélectionnez votre type de contrat électricité."
    )
    
    # Paramètres tarifaires par défaut
    if offre_france_selectionnee == "Tarif bleu particulier":
        paliers_particulier = [6, 9, 12, 15, 18, 24, 30, 36]
        # Recherche du palier le plus proche de la valeur actuelle ou 36 par défaut
        idx_defaut = 7 # 36 kVA
        if "abonnement_val_prec" in st.session_state:
            val_prec = st.session_state.abonnement_val_prec
            if val_prec in paliers_particulier:
                idx_defaut = paliers_particulier.index(val_prec)
        
        abonnement_val = st.sidebar.selectbox(
            "Abonnement (kVA)",
            paliers_particulier,
            index=idx_defaut,
            on_change=reset_simulation,
            help="Puissance souscrite de votre abonnement électrique (Tarif Bleu Particulier)."
        )
        st.session_state.abonnement_val_prec = abonnement_val
        
        # Données de l'image pour le Tarif Bleu Particulier (HC)
        tarifs_abo = {
            6: 141.60,
            9: 176.16,
            12: 209.16,
            15: 239.88,
            18: 271.80,
            24: 340.20,
            30: 402.36,
            36: 465.00
        }
        # Prix HP/HC de l'image (en c€/kWh -> conversion en €/kWh)
        # HP: 14.12 c€ -> 0.1412 €
        # HC: 10.07 c€ -> 0.1007 €
        prix_achat_hp_defaut = 0.1412
        prix_achat_hc_defaut = 0.1007
        cout_abonnement_annuel = tarifs_abo.get(abonnement_val, 465.00)
        # On ajoute la majoration pour autoproducteurs avec injection (9.60 €/an)
        cout_abonnement_annuel += 9.60
        
        # On calcule le coût moyen par kVA pour rester compatible avec la logique actuelle
        cout_abonnement_kva_defaut = cout_abonnement_annuel / abonnement_val if abonnement_val > 0 else 30.0
        
    elif offre_france_selectionnee == "Tarif bleu pro":
        option_pro = st.sidebar.radio(
            "Option tarifaire",
            ["Base", "Heures Creuses"],
            index=0,
            horizontal=True,
            on_change=reset_simulation
        )
        
        paliers_pro = [3, 6, 9, 12, 15, 18, 24, 30, 36] if option_pro == "Base" else [6, 9, 12, 15, 18, 24, 30, 36]
        
        idx_defaut = len(paliers_pro) - 1 # 36 kVA par défaut
        if "abonnement_val_pro_prec" in st.session_state:
            val_prec = st.session_state.abonnement_val_pro_prec
            if val_prec in paliers_pro:
                idx_defaut = paliers_pro.index(val_prec)
        
        abonnement_val = st.sidebar.selectbox(
            "Abonnement (kVA)",
            paliers_pro,
            index=idx_defaut,
            on_change=reset_simulation,
            help=f"Puissance souscrite de votre abonnement électrique (Tarif Bleu Pro {option_pro})."
        )
        st.session_state.abonnement_val_pro_prec = abonnement_val
        
        if option_pro == "Base":
            # Données de l'image : Tarif Bleu Pro Base
            tarifs_abo = {3: 134.04, 6: 166.92, 9: 198.60, 12: 230.28, 15: 261.48, 18: 291.60, 24: 357.36, 30: 422.52, 36: 487.20}
            prix_achat_hp_defaut = 0.1274
            prix_achat_hc_defaut = 0.1274
        else:
            # Données de l'image : Tarif Bleu Pro Heures Creuses
            tarifs_abo = {6: 167.40, 9: 200.16, 12: 233.76, 15: 265.68, 18: 299.04, 24: 371.40, 30: 436.32, 36: 501.84}
            prix_achat_hp_defaut = 0.1351
            prix_achat_hc_defaut = 0.0989
            
        cout_abonnement_annuel = tarifs_abo.get(abonnement_val, 501.84)
        cout_abonnement_annuel += 9.60 # Majoration autoproducteur
        cout_abonnement_kva_defaut = cout_abonnement_annuel / abonnement_val if abonnement_val > 0 else 30.0
        
    elif offre_france_selectionnee == "Tarif jaune":
        version_jaune = st.sidebar.radio(
            "Version d'utilisation",
            ["Longue Utilisation (LU)", "Courte Utilisation (CU)"],
            index=0,
            horizontal=True,
            on_change=reset_simulation
        )
        
        abonnement_val = st.sidebar.number_input(
            "Abonnement (kVA)",
            min_value=37.0,
            value=850.0,
            step=1.0,
            format="%.0f",
            on_change=reset_simulation,
            help="Puissance souscrite de votre abonnement électrique (Tarif Jaune > 36 kVA)."
        )
        
        # Tarification selon l'image
        if "Longue Utilisation" in version_jaune:
            prime_fixe = 38.27
            # Prix moyen approximatif pour l'affichage (le moteur utilisera le vecteur saisonnier)
            prix_achat_hp_defaut = 0.13155 # Moyenne (17.594 + 8.716)/2 / 100
            prix_achat_hc_defaut = 0.10015 # Moyenne (12.009 + 8.021)/2 / 100
        else:
            prime_fixe = 26.44
            prix_achat_hp_defaut = 0.13823
            prix_achat_hc_defaut = 0.10403
            
        cout_abonnement_annuel = abonnement_val * (prime_fixe + 1.95) # Prime fixe + Majoration autoproducteur
        cout_abonnement_kva_defaut = prime_fixe + 1.95
        taxe_puissance_annuelle = 12.41 # Tarif dépassement horaire

    elif offre_france_selectionnee == "Tarif vert":
        st.sidebar.info("La modélisation du Tarif Vert est en cours de développement.")
        abonnement_val = st.sidebar.number_input(
            "Abonnement (kVA)",
            min_value=37.0,
            value=850.0,
            step=10.0,
            format="%.0f",
            on_change=reset_simulation
        )
        cout_abonnement_kva_defaut = 30.0
        prix_achat_hp_defaut = 0.25
        prix_achat_hc_defaut = 0.20

    st.sidebar.markdown("""
        <div style="font-size: 0.8rem; color: #666; margin-top: -10px; margin-bottom: 10px;">
            ℹ️ Si vous ne connaissez pas votre introduction, prenez la même valeur que votre abonnement.
        </div>
        """, unsafe_allow_html=True)
else:
    with col_unit:
        unite_intro = st.selectbox(
            "Unité", 
            ["Ampères", "kVA"], 
            index=0,
            label_visibility="collapsed",
            on_change=reset_simulation,
            help="L'unité de puissance d'introduction de votre bâtiment."
        )
    with col_val:
        intro_val = st.number_input(
            f"Valeur Intro", 
            min_value=0.1, 
            value=250.0, 
            step=0.1 if unite_intro == "kVA" else 1.0, 
            format="%.1f" if unite_intro == "kVA" else "%.0f",
            label_visibility="collapsed",
            on_change=reset_simulation,
            help="La valeur des kVA est normalement notée dans votre contrat d'abonnement ou sur vos factures d'électricité."
        )
    abonnement_val = intro_val if unite_intro == "kVA" else (400 * intro_val * 1.732) / 1000

# --- ÉTAPE 5 : CONSOMMATION ÉNERGÉTIQUE ---
st.sidebar.write("📈 **Étape 4 : Consommation énergétique**")

profil_conso = st.sidebar.selectbox(
    "Type de bâtiment",
    ["Résidentiel", "Tertiaire / Bureaux", "Industriel"],
    index=2,
    on_change=reset_simulation
)

scénario_investissement = st.sidebar.radio(
    "Scénario d'investissement",
    ["Je suis propriétaire et consommateur sur site", "Je suis propriétaire et mon bâtiment est en location"],
    on_change=reset_simulation,
    help="1. Propriétaire-consommateur : Vous investissez et réduisez votre propre facture. \n2. Propriétaire-bailleur : Vous investissez et revendez l'électricité à vos locataires."
)

# Modes disponibles : on enlève l'estimation pour l'industriel
modes_disponibles = ["Saisie manuelle (kWh)", "Télécharger une courbe de charge"]
if profil_conso != "Industriel":
    modes_disponibles.insert(0, "Estimation automatique")

mode_conso = st.sidebar.radio(
    "Données de consommation",
    modes_disponibles,
    index=len(modes_disponibles)-1,
    horizontal=False,
    on_change=reset_simulation
)

conso_annuelle_kwh = 0
df_courbe_charge = None

if mode_conso == "Estimation automatique":
    if profil_conso == "Résidentiel":
        col_nb, col_surf = st.sidebar.columns(2)
        with col_nb:
            nb_logements = st.number_input("Nb logements", min_value=1, value=1, step=1, on_change=reset_simulation)
        with col_surf:
            surf_hab = st.number_input("Surface totale (m²)", min_value=1, value=100, step=10, on_change=reset_simulation)
            st.markdown(f'<div style="font-size: 0.8rem; color: #666; margin-top: -15px; margin-bottom: 10px;">👉 {surf_hab:,.0f} m²</div>'.replace(",", " "), unsafe_allow_html=True)
        
        col_dpe, col_heat = st.sidebar.columns(2)
        with col_dpe:
            dpe = st.selectbox("DPE", ["A", "B", "C", "D", "E", "F", "G"], index=3, on_change=reset_simulation)
        with col_heat:
            type_chauffe = st.selectbox("Chauffage", ["Électrique (PAC/Rad)", "Gaz", "Mazout"], on_change=reset_simulation)
        
        ecs_elec = st.sidebar.checkbox("Eau Chaude Sanitaire Électrique", value=True, on_change=reset_simulation)
        
        # Logique estimation Résidentiel (Ratios simplifiés)
        ratio_dpe = {"A": 50, "B": 80, "C": 120, "D": 190, "E": 250, "F": 330, "G": 450}[dpe]
        conso_base = 3000 * nb_logements # Base élec hors chauffage
        if "Électrique" in type_chauffe:
            conso_annuelle_kwh = (surf_hab * ratio_dpe) + conso_base
        else:
            conso_annuelle_kwh = conso_base + (1000 * nb_logements if ecs_elec else 0)

    elif profil_conso == "Tertiaire / Bureaux":
        surf_tert = st.sidebar.number_input("Surface totale (m²)", min_value=1, value=500, step=50, on_change=reset_simulation)
        st.sidebar.markdown(f'<div style="font-size: 0.8rem; color: #666; margin-top: -15px; margin-bottom: 10px;">👉 {surf_tert:,.0f} m²</div>'.replace(",", " "), unsafe_allow_html=True)
        activite = st.sidebar.selectbox("Activité", ["Bureaux", "Commerce", "Restauration"], on_change=reset_simulation)
        clim = st.sidebar.checkbox("Locaux climatisés", value=True, on_change=reset_simulation)
        
        # Logique estimation Tertiaire
        ratio_act = {"Bureaux": 120, "Commerce": 180, "Restauration": 350}[activite]
        if clim: ratio_act *= 1.2
        conso_annuelle_kwh = surf_tert * ratio_act

    st.sidebar.info(f"Consommation estimée : {conso_annuelle_kwh:,.0f} kWh/an".replace(",", " "))

elif mode_conso == "Saisie manuelle (kWh)":
    valeur_par_defaut = 300000 if profil_conso == "Industriel" else 5000
    conso_annuelle_kwh = st.sidebar.number_input(
        "Consommation annuelle totale (kWh)",
        min_value=0,
        value=int(valeur_par_defaut),
        step=1000 if profil_conso == "Industriel" else 100,
        format="%d",
        on_change=reset_simulation
    )
    
    if profil_conso == "Industriel":
        st.sidebar.markdown("""
            <div style="font-size: 0.8rem; color: #856404; background-color: #fff3cd; padding: 10px; border-radius: 5px; border-left: 5px solid #ffeeba; margin-top: 5px;">
                💡 Par défaut, nous considérons 300 MWh pour un site industriel. Vous pouvez ajuster cette valeur ci-dessus.
            </div>
            """, unsafe_allow_html=True)
    
        # Affichage avec séparateur de milliers
        st.sidebar.markdown(f"""
            <div style="font-size: 0.9rem; font-weight: bold; color: #155724; margin-top: 5px;">
                👉 {conso_annuelle_kwh:,.0f} kWh/an
            </div>
            """.replace(",", " "), unsafe_allow_html=True)

# Définition de la courbe de conso avant le bouton simuler
if mode_conso == "Saisie manuelle (kWh)":
    courbe_conso = generer_profil_synthetique(profil_conso, conso_annuelle_kwh)
elif mode_conso == "Estimation automatique":
    courbe_conso = generer_profil_synthetique(profil_conso, conso_annuelle_kwh)
else:
    if "courbe_charge_file" in st.session_state and st.session_state.courbe_charge_file:
        courbe_conso = st.session_state.courbe_charge_file
        conso_annuelle_kwh = sum(courbe_conso)
    else:
        courbe_conso = [0.0] * 8760
        conso_annuelle_kwh = 0

# --- ÉTAPE 5 : PARAMÈTRES FINANCIERS ---
st.sidebar.write("💰 **Étape 5 : Paramètres financiers**")

# Calcul du CAPEX PV estimé selon le type de toiture
coef_suisse = 1.1 if st.session_state.pays_selectionne == "Suisse" else 1.0

capex_pv_estime = 850 * coef_suisse # Base par défaut (Plat simple)
if type_toit == "Plat":
    if materiau == "Gravier":
        capex_pv_estime = 893 * coef_suisse
    elif materiau == "Bitumineux":
        capex_pv_estime = 935 * coef_suisse
else:  # Incliné
    if materiau == "Tôle":
        capex_pv_estime = 978 * coef_suisse
    elif materiau == "Tuile":
        capex_pv_estime = 1020 * coef_suisse
    elif materiau == "Eternit":
        capex_pv_estime = 1063 * coef_suisse

col_achat, col_vente = st.sidebar.columns(2)
with col_achat:
    if "location" in scénario_investissement:
        prix_achat = 0.0 # On ne paye pas le réseau dans ce cas
        prix_revente_locataire = st.number_input(f"Tarif vente au locataire ({devise}/kWh)", min_value=0.0, value=0.20, step=0.01, on_change=reset_simulation)
    else:
        # Valeurs par défaut dynamiques si Tarif Bleu Particulier
        if st.session_state.pays_selectionne == "France" and offre_france_selectionnee == "Tarif bleu particulier":
            prix_achat = st.number_input(f"Prix Achat électricité ({devise}/kWh)", min_value=0.0, value=prix_achat_hp_defaut, step=0.0001, format="%.4f", on_change=reset_simulation, help="Prix Heures Pleines pour le Tarif Bleu Particulier.")
        else:
            prix_achat = st.number_input(f"Prix Achat électricité ({devise}/kWh)", min_value=0.0, value=0.25, step=0.01, on_change=reset_simulation)
        prix_revente_locataire = 0.0
with col_vente:
    prix_vente = st.number_input(f"Prix vente surplus électricité ({devise}/kWh)", min_value=0.0, value=0.05, step=0.01, on_change=reset_simulation)

duree_projet = st.sidebar.number_input("Durée de vie du projet (ans)", min_value=1, max_value=50, value=25, on_change=reset_simulation)

# --- COÛTS D'INSTALLATION (REPLACÉS DANS ÉTAPE 5) ---
st.sidebar.write("**Investissement initial**")
capex_pv_unit = st.sidebar.number_input(f"Investissement PV ({devise}/kWc)", min_value=0, value=int(capex_pv_estime), step=50, on_change=reset_simulation, help=f"Valeur estimée pour une toiture {type_toit.lower()} {materiau.lower()}.")
if st.session_state.get("simuler_batterie", True):
    capex_batt_unit = st.sidebar.number_input(f"Investissement Batterie ({devise}/kWh)", min_value=0, value=int(350 * coef_suisse), step=50, on_change=reset_simulation)
else:
    capex_batt_unit = 0.0

st.sidebar.write("**Frais opérationnels annuels**")
opex_pv_unit = st.sidebar.number_input(f"Frais opérationnels PV ({devise}/kWc/an)", min_value=0.0, value=6.0 * coef_suisse, step=1.0, on_change=reset_simulation)
if st.session_state.get("simuler_batterie", True):
    opex_batt_unit = st.sidebar.number_input(f"Frais opérationnels Batterie ({devise}/kWh/an)", min_value=0.0, value=3.0 * coef_suisse, step=1.0, on_change=reset_simulation)
else:
    opex_batt_unit = 0.0

# Conversion kVA ou Amp en kW
# En résidentiel/tertiaire, 1 kVA ≈ 1 kW.
if unite_intro == "Ampères":
    puissance_intro_kw = (400 * intro_val * 1.732) / 1000
else:
    puissance_intro_kw = intro_val

# --- OBJECTIF DU SYSTÈME ---
st.sidebar.markdown("<h3 style='font-size: 1.2rem; font-weight: bold;'>Objectif du système</h3>", unsafe_allow_html=True)
mode_ideal = st.sidebar.radio(
    "Objectif du système",
    [
        "Favoriser l'autonomie sur site",
        "Favoriser l'investissement (ROI < 7,5 ans)"
    ],
    index=1,
    label_visibility="collapsed",
    on_change=reset_simulation,
    help="**Autonomie** : Max d'autoproduction avec économies optimales.\n\n**Investissement** : Max d'économies avec un ROI inférieur à 7,5 ans."
)

if "optimiser_avec_options" not in st.session_state:
    st.session_state.optimiser_avec_options = False

def toggle_optim_options():
    st.session_state.optimiser_avec_options = not st.session_state.optimiser_avec_options
    reset_simulation()

st.sidebar.checkbox(
    "Optimiser le système idéal avec les options avancées",
    value=st.session_state.optimiser_avec_options,
    on_change=toggle_optim_options,
    help="Si coché, la recherche du système idéal (PV/Batterie) tiendra compte des revenus d'écrêtage, d'arbitrage et des services systèmes."
)
optimiser_avec_options_val = st.session_state.optimiser_avec_options

simuler_batterie = st.sidebar.toggle("Simuler une batterie", value=True, key="simuler_batterie", on_change=reset_simulation)

# Options avancées batterie
autoriser_ecretage = False
autoriser_services = False
revenu_services_unit = 100000.0
taxe_puissance_annuelle = 7.0 if st.session_state.pays_selectionne == "Suisse" else 6.0
cout_abonnement_kva = 30.0 # Valeur par défaut

if simuler_batterie:
    st.sidebar.markdown("<div style='margin-left: 20px;'>", unsafe_allow_html=True)
    autoriser_ecretage = st.sidebar.checkbox("Autoriser l'écrêtement de pointe", value=False, on_change=reset_simulation)
    if autoriser_ecretage:
        if st.session_state.pays_selectionne == "France":
            # Pour la France, les tarifs et seuils sont désormais automatiques
            st.sidebar.info("🎯 Lissage des pointes actif : décharge progressive 50/50 (Batterie/Réseau).")
            
            # Tarifs par défaut selon l'offre
            if offre_france_selectionnee == "Tarif jaune":
                taxe_puissance_annuelle = 12.41
                cout_abonnement_kva = cout_abonnement_kva_defaut
            else: # Tarifs Bleus
                taxe_puissance_annuelle = 12.41 if abonnement_val >= 36 else 0.0
                cout_abonnement_kva = cout_abonnement_kva_defaut
            
            if abonnement_val < 36:
                st.sidebar.info("💡 Pour les abonnements < 36 kVA, le peak shaving permet principalement d'éviter les disjonctions (pas de gain financier direct sur les dépassements).")
            
            # Paramètres internes automatiques pour la France
            mode_ecretage = "Optimiser l'abonnement"
            nouvel_abonnement = abonnement_val
        else: # Suisse
            mode_ecretage = "Réduire les dépassements"
            nouvel_abonnement = abonnement_val
            taxe_puissance_annuelle = st.sidebar.number_input(f"Taxe puissance ({devise}/kW/mois)", min_value=0.0, value=7.0, step=0.5, format="%.1f", on_change=reset_simulation, help="Chaque mois est facturé un montant basé sur la puissance maximale atteinte chaque mois.")
            st.sidebar.markdown(f'<div style="font-size: 0.8rem; color: #666; margin-top: -15px; margin-bottom: 10px;">👉 {taxe_puissance_annuelle:,.1f} {devise}/kW/mois</div>'.replace(",", " "), unsafe_allow_html=True)
    else:
        mode_ecretage = "Réduire les dépassements"
        nouvel_abonnement = abonnement_val
        taxe_puissance_annuelle = 0.0
        cout_abonnement_kva = 0.0
    
    autoriser_services = st.sidebar.checkbox("Autoriser la participation aux services systèmes", value=False, on_change=reset_simulation)
    if autoriser_services:
        revenu_services_unit = st.sidebar.number_input(f"Revenu services systèmes ({devise}/MWh/an)", min_value=0, value=100000, step=1000, format="%d", on_change=reset_simulation)
        st.sidebar.markdown(f'<div style="font-size: 0.8rem; color: #666; margin-top: -15px; margin-bottom: 10px;">👉 {revenu_services_unit:,.0f} {devise}/MWh/an</div>'.replace(",", " "), unsafe_allow_html=True)

    autoriser_arbitrage = st.sidebar.checkbox("Autoriser l'arbitrage sur le marché Spot", value=False, on_change=reset_simulation, help="Si activé, la batterie se chargera sur le réseau pendant les heures où les prix du marché sont les plus bas pour restituer l'énergie pendant les pics de prix.")
    if autoriser_arbitrage:
        st.sidebar.info("📈 L'arbitrage utilise les prix dynamiques du marché Spot.")
        autoriser_prix_dynamiques = True 
    else:
        autoriser_prix_dynamiques = False

    st.sidebar.markdown("</div>", unsafe_allow_html=True)

# --- BOUTON SIMULER ---
st.sidebar.write("")

btn_lancer_simulation = st.sidebar.button("🚀 Simuler", use_container_width=True, type="primary")

if "simulation_lancee" not in st.session_state:
    st.session_state.simulation_lancee = False
if "parametres_modifies" not in st.session_state:
    st.session_state.parametres_modifies = False

if btn_lancer_simulation:
    st.session_state.simulation_lancee = True
    st.session_state.parametres_modifies = False
    
    # Figer les paramètres pour la simulation
    st.session_state.params_valides = {
        "adresse": adresse,
        "type_toit": type_toit,
        "materiau": materiau,
        "donnees_pans": [p.copy() for p in donnees_pans],
        "mode_mesure": mode_mesure,
        "unite_intro": unite_intro,
        "intro_val": intro_val,
        "abonnement_val": abonnement_val,
        "offre_france": offre_france_selectionnee if st.session_state.pays_selectionne == "France" else None,
        "prix_achat_hc_defaut": prix_achat_hc_defaut if st.session_state.pays_selectionne == "France" and offre_france_selectionnee == "Tarif bleu particulier" else None,
        "profil_conso": profil_conso,
        "scénario_investissement": scénario_investissement,
        "conso_annuelle_kwh": conso_annuelle_kwh,
        "prix_achat": prix_achat,
        "prix_vente": prix_vente,
        "prix_revente_locataire": prix_revente_locataire,
        "duree_projet": duree_projet,
        "capex_pv_unit": capex_pv_unit,
        "capex_batt_unit": capex_batt_unit,
        "opex_pv_unit": opex_pv_unit,
        "opex_batt_unit": opex_batt_unit,
        "simuler_batterie": simuler_batterie,
        "mode_ideal": mode_ideal,
        "optimiser_avec_options": optimiser_avec_options_val,
        "autoriser_prix_dynamiques": autoriser_prix_dynamiques if 'autoriser_prix_dynamiques' in locals() else False,
        "autoriser_ecretage": autoriser_ecretage,
        "autoriser_services": autoriser_services,
        "autoriser_arbitrage": autoriser_arbitrage if 'autoriser_arbitrage' in locals() else False,
        "revenu_services_unit": revenu_services_unit,
        "taxe_puissance_annuelle": taxe_puissance_annuelle,
        "cout_abonnement_kva": cout_abonnement_kva,
        "courbe_conso": courbe_conso.copy() if 'courbe_conso' in locals() else None,
        "devise": devise,
        "variante_plat": variante_plat if 'variante_plat' in locals() else None,
        "version_jaune": version_jaune if 'version_jaune' in locals() else None,
        "option_pro": option_pro if 'option_pro' in locals() else None
    }

if mode_conso == "Télécharger une courbe de charge":
    fichier_conso = st.sidebar.file_uploader(
        "Télécharger votre courbe de charge (CSV ou Excel)",
        type=["csv", "xlsx"],
        help="Format requis : Un fichier avec deux colonnes (Date/Heure et Valeur). L'outil détecte automatiquement si les données sont en kW (Puissance) ou kWh (Énergie). Supporte les pas de 15 min, 30 min ou 1h."
    )
    if fichier_conso:
        try:
            if fichier_conso.name.endswith('.csv'):
                # On essaie les séparateurs courants en Europe (; ou ,)
                try:
                    df_courbe_charge = pd.read_csv(fichier_conso, sep=';')
                    if len(df_courbe_charge.columns) < 2:
                        fichier_conso.seek(0)
                        df_courbe_charge = pd.read_csv(fichier_conso, sep=',')
                except:
                    fichier_conso.seek(0)
                    df_courbe_charge = pd.read_csv(fichier_conso, sep=',')
            else:
                df_courbe_charge = pd.read_excel(fichier_conso)
            
            if df_courbe_charge is not None:
                # --- LOGIQUE DE DÉTECTION AUTO ULTRA-ROBUSTE ---
                # 1. Nettoyage global du DataFrame (remplacer virgules par points et supprimer espaces)
                df_courbe_charge = df_courbe_charge.astype(str).apply(lambda x: x.str.replace(',', '.').str.strip())
                
                # 2. On tente de convertir chaque colonne en numérique
                for col in df_courbe_charge.columns:
                    df_courbe_charge[col] = pd.to_numeric(df_courbe_charge[col], errors='coerce')
                
                cols_numeriques = df_courbe_charge.select_dtypes(include=[np.number]).columns
                
                # On enlève les colonnes qui ne sont que des NaN
                cols_numeriques = [c for c in cols_numeriques if df_courbe_charge[c].notna().any()]
                
                if len(cols_numeriques) > 0:
                    # On prend la colonne numérique qui a la plus grande somme (pour éviter les index/dates)
                    col_val = df_courbe_charge[cols_numeriques].sum().idxmax()
                    
                    # On nettoie les NaN et on remplace par 0 pour le calcul de la somme
                    df_final = df_courbe_charge[col_val].fillna(0)
                    
                    nb_points = len(df_final)
                    vals = df_final.tolist()

                    # Détection auto du pas de temps
                    if nb_points > 30000: # Quart-horaire
                        courbe_conso_horaire = [sum(vals[i:i+4]) for i in range(0, len(vals), 4)][:8760]
                    elif nb_points > 8000: # Horaire
                        courbe_conso_horaire = vals[:8760]
                    else:
                        courbe_conso_horaire = vals
                    
                    # Compléter à 8760h
                    if len(courbe_conso_horaire) < 8760:
                        courbe_conso_horaire.extend([0.0] * (8760 - len(courbe_conso_horaire)))
                    
                    conso_totale_det = sum(courbe_conso_horaire)
                    
                    # Message de succès avec conso totale et police réduite
                    if conso_totale_det > 100000:
                        msg_conso = f"{conso_totale_det/1000:,.0f} MWh/an"
                    else:
                        msg_conso = f"{conso_totale_det:,.0f} kWh/an"
                        
                    st.sidebar.markdown(f"""
                        <div style="font-size: 0.8rem; color: #155724; background-color: #d4edda; padding: 10px; border-radius: 5px; border-left: 5px solid #28a745; margin-top: 10px;">
                            ✅ Consommation annuelle détectée : {msg_conso}
                        </div>
                        """, unsafe_allow_html=True)
                    
                    # Stockage persistant
                    st.session_state.courbe_charge_file = courbe_conso_horaire
                    st.session_state.conso_calculee = conso_totale_det
        except Exception as e:
            st.sidebar.error(f"Erreur lors de la lecture du fichier : {e}")

if st.session_state.get("simulation_lancee", False) and "params_valides" in st.session_state:
    # Récupération des paramètres figés
    pv = st.session_state.params_valides
    adresse_val = pv["adresse"]
    type_toit_val = pv["type_toit"]
    materiau_val = pv["materiau"]
    donnees_pans_val = pv["donnees_pans"]
    mode_mesure_val = pv["mode_mesure"]
    unite_intro_val = pv["unite_intro"]
    intro_val_val = pv["intro_val"]
    abonnement_val_val = pv.get("abonnement_val", intro_val_val)
    offre_france_val = pv.get("offre_france")
    prix_achat_hc_defaut_val = pv.get("prix_achat_hc_defaut")
    profil_conso_val = pv["profil_conso"]
    scénario_investissement_val = pv["scénario_investissement"]
    conso_annuelle_kwh_val = pv["conso_annuelle_kwh"]
    prix_achat_val = pv["prix_achat"]
    prix_vente_val = pv["prix_vente"]
    prix_revente_locataire_val = pv["prix_revente_locataire"]
    duree_projet_val = pv["duree_projet"]
    capex_pv_unit_val = pv["capex_pv_unit"]
    capex_batt_unit_val = pv["capex_batt_unit"]
    opex_pv_unit_val = pv["opex_pv_unit"]
    opex_batt_unit_val = pv["opex_batt_unit"]
    simuler_batterie_val = pv["simuler_batterie"]
    mode_ideal_val = pv["mode_ideal"]
    optimiser_avec_options_val = pv.get("optimiser_avec_options", False)
    autoriser_prix_dynamiques_val = pv.get("autoriser_prix_dynamiques", False)
    autoriser_ecretage_val = pv.get("autoriser_ecretage", False)
    autoriser_services_val = pv.get("autoriser_services", False)
    autoriser_arbitrage_val = pv.get("autoriser_arbitrage", False)
    prix_hc_val = pv.get("prix_hc", 0.0)
    version_jaune_val = pv.get("version_jaune")
    option_pro_val = pv.get("option_pro")
    hc_start_val = pv.get("hc_start", 0)
    hc_end_val = pv.get("hc_end", 0)
    revenu_services_unit_val = pv.get("revenu_services_unit", 100000.0)
    taxe_puissance_annuelle_val = pv.get("taxe_puissance_annuelle", 6.0)
    cout_abonnement_kva_val = pv.get("cout_abonnement_kva", 30.0)
    courbe_conso_val = pv["courbe_conso"]
    devise_val = pv["devise"]
    variante_plat_val = pv["variante_plat"]

    # --- RÉCUPÉRATION PRIX DYNAMIQUES ---
    if autoriser_prix_dynamiques_val:
        vecteur_prix_achat = recuperer_prix_dynamiques(st.session_state.get("pays_selectionne", "France"))
    elif offre_france_val == "Tarif jaune":
        # Création d'un vecteur saisonnier et HP/HC pour le Tarif Jaune
        # Hiver : Nov, Dec, Jan, Feb, Mar (Mois 11, 12, 1, 2, 3)
        # Eté : Apr, May, Jun, Jul, Aug, Sep, Oct (Mois 4, 5, 6, 7, 8, 9, 10)
        # HP: 6h-22h / HC: 22h-6h
        
        is_lu = "Longue Utilisation" in version_jaune_val if version_jaune_val else True
        
        # Tarifs (en €/kWh)
        if is_lu:
            p_hiver_hp, p_hiver_hc = 0.17594, 0.12009
            p_ete_hp, p_ete_hc = 0.08716, 0.08021
        else:
            p_hiver_hp, p_hiver_hc = 0.18808, 0.12751
            p_ete_hp, p_ete_hc = 0.08839, 0.08056
            
        vecteur_prix_achat = []
        for h in range(8760):
            heure = h % 24
            # On approxime le mois (30 jours par mois)
            mois = ((h // (24 * 30)) % 12) + 1
            
            est_hiver = mois in [1, 2, 3, 11, 12]
            est_hp = 6 <= heure < 22
            
            if est_hiver:
                vecteur_prix_achat.append(p_hiver_hp if est_hp else p_hiver_hc)
            else:
                vecteur_prix_achat.append(p_ete_hp if est_hp else p_ete_hc)

    elif offre_france_val == "Tarif bleu particulier" and prix_achat_hc_defaut_val:
        # Création d'un vecteur HP/HC pour le Tarif Bleu Particulier
        # Heures creuses Enedis typiques: 22h-6h (8h de HC)
        vecteur_prix_achat = []
        for h in range(8760):
            heure = h % 24
            if 6 <= heure < 22: # Heures Pleines (16h)
                vecteur_prix_achat.append(prix_achat_val)
            else: # Heures Creuses (8h)
                vecteur_prix_achat.append(prix_achat_hc_defaut_val)
    else:
        vecteur_prix_achat = [prix_achat_val] * 8760

    lat, lon = obtenir_lat_lon(adresse_val)

    # Autoriser la simulation si lat/lon sont trouvés OU si on est en Suisse (avec repli par défaut)
    if (lat and lon) or st.session_state.get("pays_selectionne") == "Suisse":
        # --- LOGIQUE DE DIMENSIONNEMENT PV PRÉCISE (MAJ ERP) ---
        largeur_base = 1.134
        longueur_base = 1.961
        espacement_fixation = 0.02 # 2cm total (1cm de chaque côté entre panneaux)
        pourtour_erp = 0.90 # 90cm de sécurité ERP sur tout le périmètre
        
        puissance_pv_installable = 0
        production_totale_an = 0
        nb_modules_total = 0
        details_pans_calcul = []
        prod_mensuelle_cumulee = [0.0] * 12
        prod_horaire_cumulee = [0.0] * 8760
        ecartement_calcule = 0

        for pan in donnees_pans_val:
            # ... (logique existante conservée pour nb_mods, puissance_pan, etc.)
            incli_pan = pan['inclinaison']
            surf_pan = pan['surface']
            orient_pan = pan['orientation']
            
            if mode_mesure_val == "Vue aérienne":
                surf_reelle = surf_pan / math.cos(math.radians(incli_pan))
            else:
                surf_reelle = surf_pan

            cote_theorique = math.sqrt(surf_reelle)
            surf_utile = (cote_theorique - 2 * pourtour_erp)**2 if cote_theorique > 1.8 else 0
            
            if type_toit_val == "Plat":
                dim_long = longueur_base + espacement_fixation
                dim_larg = largeur_base + espacement_fixation
                larg_projetee = dim_larg * math.cos(math.radians(10))
                ecartement_optimal = 0.10 if variante_plat_val and "Est-Ouest" in variante_plat_val else 0.45
                surf_par_module = dim_long * (larg_projetee + ecartement_optimal)
                ecartement_calcule = ecartement_optimal
            else:
                dim_long = longueur_base + espacement_fixation
                dim_larg = largeur_base + espacement_fixation
                surf_par_module = dim_long * dim_larg

            nb_mods = int(surf_utile / surf_par_module)
            puissance_pan = nb_mods * 0.5
            
            aspect = get_aspect(orient_pan)
            prod_unit = appeler_pvgis(lat, lon, incli_pan, aspect)
            prod_mensuelle_unitaire = appeler_pvgis_mensuel(lat, lon, incli_pan, aspect)
            prod_horaire_unitaire = appeler_pvgis_horaire(lat, lon, incli_pan, aspect)
            
            # Gestion du repli pour la Suisse si PVGIS échoue ou si lat/lon sont nuls
            if not prod_unit and st.session_state.get("pays_selectionne") == "Suisse":
                prod_unit = 1020  # Valeur par défaut demandée
                # Profil mensuel synthétique (répartition typique Europe Centrale)
                prod_mensuelle_unitaire = [
                    0.03*prod_unit, 0.05*prod_unit, 0.08*prod_unit, 0.11*prod_unit, 
                    0.13*prod_unit, 0.14*prod_unit, 0.14*prod_unit, 0.12*prod_unit, 
                    0.09*prod_unit, 0.06*prod_unit, 0.03*prod_unit, 0.02*prod_unit
                ]
                # Profil horaire synthétique simple (cloche journalière)
                prod_horaire_unitaire = []
                for d in range(365):
                    for h in range(24):
                        # Cloche simplifiée entre 6h et 20h
                        val = max(0, math.sin(math.pi * (h - 6) / 14)) if 6 <= h <= 20 else 0
                        prod_horaire_unitaire.append(val)
                # Normalisation du profil horaire
                s_h = sum(prod_horaire_unitaire)
                if s_h > 0:
                    prod_horaire_unitaire = [p * (prod_unit / s_h) for p in prod_horaire_unitaire]

            if prod_unit:
                nb_modules_total += nb_mods
                puissance_pv_installable += puissance_pan
                production_totale_an += puissance_pan * prod_unit
                
                if prod_mensuelle_unitaire:
                    for i in range(12):
                        prod_mensuelle_cumulee[i] += prod_mensuelle_unitaire[i] * puissance_pan
                
                if prod_horaire_unitaire:
                    for i in range(8760):
                        prod_horaire_cumulee[i] += prod_horaire_unitaire[i] * puissance_pan

                details_pans_calcul.append({
                    "orientation": orient_pan,
                    "inclinaison": incli_pan,
                    "surface": surf_pan,
                    "puissance": puissance_pan,
                    "prod_unit": prod_unit,
                    "nb_mods": nb_mods
                })

        # Conversion kVA ou Amp en kW (avec paramètres valides)
        if unite_intro_val == "Ampères":
            puissance_intro_kw_val = (400 * intro_val_val * 1.732) / 1000
        else:
            puissance_intro_kw_val = intro_val_val

        # Limitation par la puissance d'introduction
        puissance_retenue = min(puissance_pv_installable, puissance_intro_kw_val)
        
        # Correction du nombre de modules pour qu'il soit cohérent avec la puissance retenue
        nb_modules_final = int(puissance_retenue / 0.5)
        
        if puissance_pv_installable > 0:
            facteur_limite = puissance_retenue / puissance_pv_installable
            production_totale_an *= facteur_limite
            prod_mensuelle_cumulee = [p * facteur_limite for p in prod_mensuelle_cumulee]
            prod_horaire_cumulee = [p * facteur_limite for p in prod_horaire_cumulee]

        # Normalisation de la courbe horaire pour correspondre exactement au total annuel (évite les taux > 100%)
        somme_horaire = sum(prod_horaire_cumulee)
        if somme_horaire > 0 and production_totale_an > 0:
            ratio_norm = production_totale_an / somme_horaire
            prod_horaire_cumulee = [p * ratio_norm for p in prod_horaire_cumulee]
        
        # --- CALCUL AUTOCONSOMMATION ---
        # Utilisation de la courbe de conso figée
        courbe_conso_val_calc = courbe_conso_val if courbe_conso_val else [0.0] * 8760

        # Simulation heure par heure
        autoconsommation_kwh = 0
        surplus_injecte_kwh = 0
        economies_elec_s2 = 0 # On calcule les économies basées sur le vecteur prix
        for idx, (p, c) in enumerate(zip(prod_horaire_cumulee, courbe_conso_val_calc)):
            part_auto = min(p, c)
            autoconsommation_kwh += part_auto
            surplus_injecte_kwh += (p - part_auto)
            
            # Économie section 2
            if "location" in scénario_investissement_val:
                economies_elec_s2 += part_auto * prix_revente_locataire_val
            else:
                economies_elec_s2 += part_auto * vecteur_prix_achat[idx]
        
        taux_autoconsommation = (autoconsommation_kwh / production_totale_an * 100) if production_totale_an > 0 else 0
        taux_autoproduction = (autoconsommation_kwh / conso_annuelle_kwh_val * 100) if conso_annuelle_kwh_val > 0 else 0
        
        productible_moyen = production_totale_an / puissance_retenue if puissance_retenue > 0 else 0
    else:
        productible_moyen = 0
        details_pvgis = []
        lat, lon = None, None
        puissance_pv_installable = 0
        puissance_retenue = 0
        production_totale_an = 0

    # --- AFFICHAGE DES RÉSULTATS ---
    if (lat and lon) or st.session_state.get("pays_selectionne") == "Suisse":
        st.header("Bilan énergétique de votre site")
        st.write("---")
        col1, col2 = st.columns(2)

        with col1:
            st.markdown("#### **📍 Bâtiment**")
            st.write(f"**Adresse :** {adresse_val if adresse_val else 'Non spécifiée (Suisse)'}")
            
            # Affichage Introduction
            if st.session_state.get("pays_selectionne") == "France":
                # Affichage Abonnement pour la France
                st.write(f"**Abonnement :** {abonnement_val_val:,.1f} kVA".replace(",", " "))
                
                # Affichage Introduction
                if unite_intro_val == "kVA":
                    equiv_amp = (intro_val_val * 1000) / (400 * 1.732)
                    st.write(f"**Introduction :** {intro_val_val:,.1f} kVA - {int(equiv_amp):,} A".replace(",", " "))
                else:
                    equiv_kva = (400 * intro_val_val * 1.732) / 1000
                    st.write(f"**Introduction :** {equiv_kva:,.1f} kVA - {int(intro_val_val):,} A".replace(",", " "))
            else:
                # Affichage Introduction pour les autres pays
                if unite_intro_val == "kVA":
                    equiv_amp = (intro_val_val * 1000) / (400 * 1.732)
                    st.write(f"**Introduction :** {intro_val_val:,.1f} kVA - {int(equiv_amp):,} A".replace(",", " "))
                else:
                    equiv_kva = (400 * intro_val_val * 1.732) / 1000
                    st.write(f"**Introduction :** {equiv_kva:,.1f} kVA - {int(intro_val_val):,} A".replace(",", " "))

            # Affichage Consommation
            label_conso = "Consommation locataires :" if "location" in scénario_investissement_val else "Consommation :"
            if conso_annuelle_kwh_val > 100000:
                st.write(f"**{label_conso}** {int(round(conso_annuelle_kwh_val/1000)):,} MWh/an".replace(",", " "))
            else:
                st.write(f"**{label_conso}** {conso_annuelle_kwh_val:,.0f} kWh/an".replace(",", " "))
            
            p_pointe_estimee = max(courbe_conso_val_calc) if courbe_conso_val_calc else 0
            st.write(f"**Puissance de pointe estimée :** {p_pointe_estimee:,.1f} kW".replace(",", " "))

            st.write(f"**Toiture :** {type_toit_val} ({materiau_val})")
            st.write("**Potentiel par orientation :**")
            
            # Création d'un petit tableau HTML pour les orientations
            html_table = """<style>
.small-table { width: 100%; font-size: 0.8rem; border-collapse: collapse; margin-top: 5px; table-layout: fixed; }
.small-table td, .small-table th { border-bottom: 1px solid #eee; padding: 4px 0; text-align: left; width: 20%; overflow: hidden; }
.small-table th { color: #666; font-weight: normal; }
</style>
<table class="small-table">
<thead>
<tr><th>Orientation</th><th>Inclinaison</th><th>Surface</th><th>Modules</th><th>Puissance</th></tr>
</thead>
<tbody>"""
            for d in details_pans_calcul:
                html_table += f"<tr><td>{d['orientation']}</td><td>{d['inclinaison']}°</td><td>{d['surface']:,.0f} m²</td><td>{d['nb_mods']:,}</td><td>{d['puissance']:,.1f} kWc</td></tr>"
            
            html_table += "</tbody></table>"
            st.markdown(html_table.replace(",", " "), unsafe_allow_html=True)
            st.write(f"**Puissance maximale installable en toiture :** {puissance_pv_installable:,.1f} kWc".replace(",", " "))
        
        with col2:
            st.markdown("#### **☀️ Potentiel Solaire**")
            
            # 1 & 3. Puissance installable et modules sur la même ligne
            st.markdown(f'**Puissance installable :** {puissance_retenue:,.1f} kWc <span style="font-size: 0.9rem; color: #666; margin-left: 10px;">(soit {int(nb_modules_final):,} modules de 500 Wc)</span>'.replace(",", " "), unsafe_allow_html=True)
            
            # 2. La remarque en bleue
            st.markdown(f"""
                <div style="font-size: 0.9rem; color: #555; background-color: #e7f3fe; padding: 10px; border-radius: 5px; border-left: 5px solid #2196F3; margin-top: 5px; margin-bottom: 20px;">
                    💡 La puissance installable est définit comme suit : <br>
                    le <b>minimum</b> entre la <b>capacité de votre toit</b> et la puissance de votre <b>raccordement électrique</b>.
                </div>
                """, unsafe_allow_html=True)

            # 4. Productible PVGIS
            st.write(f"**Productible (PVGIS) :** {productible_moyen:,.0f} kWh/kWc/an".replace(",", " "))
            
            # 5. Production annuelle
            st.write(f"**Production annuelle totale :** {production_totale_an:,.0f} kWh/an".replace(",", " "))

        # Une seule ligne continue séparatrice après les deux paragraphes
        st.write("---")
        
        # Calcul du max pour harmoniser les axes Y des deux graphiques
        # Calcul de la conso mensuelle
        conso_mensuelle = [0.0] * 12
        jours_par_mois = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
        idx_h = 0
        for m in range(12):
            heures_mois = jours_par_mois[m] * 24
            conso_mensuelle[m] = sum(courbe_conso_val_calc[idx_h : idx_h + heures_mois])
            idx_h += heures_mois
            
        max_y_graph = max(max(conso_mensuelle) if conso_mensuelle else 0, max(prod_mensuelle_cumulee) if prod_mensuelle_cumulee else 0) * 1.1

        # Nouvelles colonnes pour les graphiques côte à côte
        col_g1, col_g2 = st.columns(2)
        
        with col_g1:
            # Graphique Conso Mensuelle
            label_graph_conso = "Consommation mensuelle locataires" if "location" in scénario_investissement_val else "Consommation mensuelle"
            st.markdown(f"<h4 style='font-size: 1.1rem; margin-bottom: 0px;'>📊 {label_graph_conso}</h4>", unsafe_allow_html=True)
            mois_noms_loc = ["Jan", "Fév", "Mar", "Avr", "Mai", "Juin", "Juil", "Août", "Sep", "Oct", "Nov", "Déc"]
            
            df_conso_m = pd.DataFrame({
                "Mois": mois_noms_loc,
                "Consommation (kWh)": conso_mensuelle
            })
            df_conso_m["Mois"] = pd.Categorical(df_conso_m["Mois"], categories=mois_noms_loc, ordered=True)
            
            fig_conso = px.bar(
                df_conso_m,
                x="Mois",
                y="Consommation (kWh)",
                color_discrete_sequence=["#AED6F1"] # BLEU PASTEL
            )
            fig_conso.update_layout(
                margin=dict(l=0, r=0, t=10, b=0),
                height=250,
                xaxis_title=None,
                yaxis_title="kWh",
                yaxis_range=[0, max_y_graph],
                font=dict(size=10)
            )
            st.plotly_chart(fig_conso, use_container_width=True, config={'displayModeBar': False})

        with col_g2:
            # Graphique Production Mensuelle
            st.markdown("<h4 style='font-size: 1.1rem; margin-bottom: 0px;'>📊 Production mensuelle</h4>", unsafe_allow_html=True)
            mois_noms_loc = ["Jan", "Fév", "Mar", "Avr", "Mai", "Juin", "Juil", "Août", "Sep", "Oct", "Nov", "Déc"]
            
            df_mensuel = pd.DataFrame({
                "Mois": mois_noms_loc,
                "Production (kWh)": prod_mensuelle_cumulee
            })
            df_mensuel["Mois"] = pd.Categorical(df_mensuel["Mois"], categories=mois_noms_loc, ordered=True)
            
            fig_prod = px.bar(
                df_mensuel, 
                x="Mois", 
                y="Production (kWh)",
                color_discrete_sequence=["#F7DC6F"] # JAUNE PASTEL
            )
            fig_prod.update_layout(
                margin=dict(l=0, r=0, t=10, b=0),
                height=250,
                xaxis_title=None,
                yaxis_title="kWh",
                yaxis_range=[0, max_y_graph],
                font=dict(size=10)
            )
            st.plotly_chart(fig_prod, use_container_width=True, config={'displayModeBar': False})

        # --- SECTION AUTOCONSOMMATION ---
        st.write("---")
        st.header("Analyse de l'autoconsommation en exploitant la totalité de votre toiture")
        st.write(f"Pour une installation photovoltaique seule de **{puissance_retenue:,.1f} kWc** conditionnée par la puissance de votre raccordement éléctrique actuel".replace(",", " "))
        
        # Calcul des KPI financiers pour la section 2
        capex_pv_s2 = puissance_retenue * capex_pv_unit_val
        opex_total_s2 = puissance_retenue * opex_pv_unit_val
        
        # Gain annuel = (Auto-consommé * Prix Achat/Locataire) + (Vendu * Prix Vente) - Maintenance
        # economies_elec_s2 est déjà calculé ci-dessus pour le PV seul
            
        vente_surplus_s2 = (surplus_injecte_kwh * prix_vente_val)
        gain_annuel_s2 = economies_elec_s2 + vente_surplus_s2 - opex_total_s2
        
        roi_s2 = capex_pv_s2 / gain_annuel_s2 if gain_annuel_s2 > 0 else 0
        economies_totales_s2 = gain_annuel_s2 * duree_projet_val
        
        col_res1, col_res2, col_res3 = st.columns(3)
        col_res1.metric("Autoconsommation", f"{taux_autoconsommation:,.1f} %".replace(",", " "), help="Part de la production PV consommée sur place.")
        col_res2.metric("Autoproduction", f"{taux_autoproduction:,.1f} %".replace(",", " "), help="Part de la consommation totale couverte par le PV.")
        col_res3.metric("Surplus rejeté", f"{surplus_injecte_kwh:,.0f} kWh".replace(",", " "), help="Énergie réinjectée sur le réseau.")
        
        # KPI Financiers sur une ligne
        f_col1, f_col2, f_col3, f_col4 = st.columns(4)
        f_col1.metric("Investissement", f"{capex_pv_s2:,.0f} {devise_val}".replace(",", " "))
        f_col2.metric("Gain annuel", f"{gain_annuel_s2:,.0f} {devise_val}/an".replace(",", " "))
        f_col3.metric("ROI", f"{roi_s2:.1f} ans")
        f_col4.metric(f"Économies ({duree_projet_val} ans)", f"{economies_totales_s2:,.0f} {devise_val}".replace(",", " "))
        
        # --- SECTION SYSTÈME IDÉAL ---
        st.write("---")
        st.header("🏆 Votre système photovoltaïque et stockage idéal")
        
        with st.expander("🛠️ Tester manuellement une configuration"):
            if simuler_batterie_val:
                col_m1, col_m2 = st.columns(2)
                p_man = col_m1.number_input("Puissance PV (kWc)", min_value=0.0, value=float(puissance_retenue), step=1.0)
                b_man = col_m2.number_input("Capacité Batterie (kWh)", min_value=0.0, value=0.0, step=1.0)
            else:
                p_man = st.number_input("Puissance PV (kWc)", min_value=0.0, value=float(puissance_retenue), step=1.0)
                b_man = 0.0
            btn_manuel = st.button("Simuler manuellement")
        
        # La simulation s'exécute automatiquement pour l'optimisation
        # ou manuellement si le bouton est cliqué
        override_ideal = btn_manuel
        
        if True: # On simule toujours (soit l'idéal auto, soit le manuel si cliqué)
            if override_ideal:
                # On utilise les valeurs saisies manuellement
                p_test_manuel = p_man
                capa_test_manuel = b_man
            
            # Paramètres batteries par défaut
            DOD = 0.90  # Profondeur de décharge (90%)
            RENDEMENT_CHARGE = 0.95
            RENDEMENT_DECHARGE = 0.95
            C_RATE = 0.5  # Puissance max = 0.5 * Capacité (Système 2h)
            
            # 1. Calcul des profils unitaires (pour 1 kWc) par orientation
            profils_unitaires_par_pan = []
            for pan in donnees_pans_val:
                aspect = get_aspect(pan['orientation'])
                prod_h_unit = appeler_pvgis_horaire(lat, lon, pan['inclinaison'], aspect)
                if prod_h_unit:
                    # Normalisation comme fait précédemment pour PVGIS 5.2
                    p_annuelle_unit = appeler_pvgis(lat, lon, pan['inclinaison'], aspect)
                    if p_annuelle_unit:
                        somme_h = sum(prod_h_unit)
                        if somme_h > 0:
                            ratio_n = p_annuelle_unit / somme_h
                            prod_h_unit = [ph * ratio_n for ph in prod_h_unit]
                    
                    # Calcul puissance max de ce pan
                    if type_toit_val == "Plat":
                        dim_long = longueur_base + espacement_fixation
                        dim_larg = largeur_base + espacement_fixation
                        larg_projetee = dim_larg * math.cos(math.radians(10))
                        ecartement_opt = 0.15 if variante_plat_val and "Est-Ouest" in variante_plat_val else 0.45
                        surf_par_mod = dim_long * (larg_projetee + ecartement_opt)
                    else:
                        dim_long = longueur_base + espacement_fixation
                        dim_larg = largeur_base + espacement_fixation
                        surf_par_mod = dim_long * dim_larg
                    
                    if mode_mesure_val == "Vue aérienne":
                        s_reelle = pan['surface'] / math.cos(math.radians(pan['inclinaison']))
                    else:
                        s_reelle = pan['surface']
                    
                    c_theo = math.sqrt(s_reelle)
                    s_utile = (c_theo - 2 * pourtour_erp)**2 if c_theo > 1.8 else 0
                    nb_m = int(s_utile / surf_par_mod)
                    p_pan_max = nb_m * 0.5
                    
                    profils_unitaires_par_pan.append({
                        "profil": prod_h_unit,
                        "p_max": p_pan_max
                    })

            # 2. Recherche du dimensionnement PV + Batterie optimal (Autonomie & Rentabilité)
            if profils_unitaires_par_pan and conso_annuelle_kwh_val > 0:
                best_autoprod_score = -float('inf')
                best_pv_total = 0
                best_capa_batt = 0
                best_gain_annuel = 0
                best_capex = 0
                best_taux_auto_config = 0
                best_taux_prod_config = 0
                best_surplus_config = 0
                best_economies = 0
                
                scenarios_comparaison = [] # Pour le graphique de synthèse

                if override_ideal:
                    # MODE MANUEL : Une seule simulation avec les valeurs saisies
                    p_test = p_test_manuel
                    cap_b = capa_test_manuel
                    
                    p_totale_max_toit = sum(p['p_max'] for p in profils_unitaires_par_pan)
                    ratio_pv = p_test / p_totale_max_toit if p_totale_max_toit > 0 else 0
                    prod_h_test = [0.0] * 8760
                    for item in profils_unitaires_par_pan:
                        p_pan_test = item['p_max'] * ratio_pv
                        for i in range(8760):
                            prod_h_test[i] += item['profil'][i] * p_pan_test
                    
                    cap_utile_b = cap_b * DOD
                    s_temp = 0.0
                    auto_temp_kwh = 0
                    p_batt_max_test = cap_b * C_RATE
                    soc_points = []
                    p_max_net = 0
                    total_decharge_sim = 0
                    p_batt_real_max = 0
                    total_charge_reseau = 0
                    
                    # Pour suivre l'origine de l'énergie dans la batterie (Solaire vs Réseau)
                    stock_solaire = 0.0 # Énergie stockée issue du PV (kWh)
                    stock_reseau = 0.0  # Énergie stockée issue du réseau (kWh)
                    
                    # Seuil pour l'arbitrage dynamique (on charge si prix < moyenne et décharge si prix > moyenne)
                    if autoriser_prix_dynamiques_val:
                        prix_moyen = sum(vecteur_prix_achat) / 8760
                        seuil_charge = prix_moyen * 0.8
                        seuil_decharge = prix_moyen * 1.1
                
                    # Calcul du surplus solaire attendu pour chaque jour (Production - Conso sur les heures de surplus)
                    surplus_journalier_attendu_m = []
                    for j in range(365):
                        s_jour = 0
                        for h in range(24):
                            idx = j*24 + h
                            s_jour += max(0, prod_h_test[idx] - courbe_conso_val_calc[idx])
                        surplus_journalier_attendu_m.append(s_jour)
                
                    for h_idx, (ph, ch) in enumerate(zip(prod_h_test, courbe_conso_val_calc)):
                        h_jour = h_idx % 24
                        j_idx = h_idx // 24
                        prix_h = vecteur_prix_achat[h_idx]
                    
                        # Détermination si prix bas pour arbitrage Spot
                        est_prix_bas = False
                        if (autoriser_arbitrage_val or autoriser_prix_dynamiques_val):
                            prix_moyen = sum(vecteur_prix_achat) / 8760
                            # Seuil de rentabilité : le prix doit être inférieur à 80% du prix moyen 
                            est_prix_bas = prix_h < (prix_moyen * 0.8)
                        
                            # Anticipation solaire STRICTE pour garantir 0 impact sur l'autoconsommation
                            # On ne charge sur le réseau la nuit QUE s'il reste de la place APRÈS le surplus solaire attendu
                            if est_prix_bas and 0 <= h_jour <= 6:
                                surplus_prevu = surplus_journalier_attendu_m[j_idx]
                                # Place libre maximale pour le réseau = Capacité utile - Surplus solaire prévu
                                place_pour_reseau = max(0, cap_utile_b - surplus_prevu)
                                if (stock_solaire + stock_reseau) >= place_pour_reseau:
                                    est_prix_bas = False
                                
                                # --- PRIORITÉ 1 : LE SOLAIRE ---
                                charge_sol = 0
                                dech_sol = 0
                                if ph >= ch:
                                    # Consommation directe du solaire
                                    auto_temp_kwh += ch
                                    dispo_solaire = ph - ch
                                    
                                    # Charger la batterie avec le surplus solaire
                                    charge_sol = min(dispo_solaire, (cap_utile_b - (stock_solaire + stock_reseau)) / RENDEMENT_CHARGE, p_batt_max_test)
                                    stock_solaire += charge_sol * RENDEMENT_CHARGE
                                    p_batt_real_max = max(p_batt_real_max, charge_sol)
                                    p_max_net = max(p_max_net, 0)
                                else:
                                    # Solaire insuffisant
                                    auto_temp_kwh += ph
                                    besoin = ch - ph
                                    
                                    # Décharge batterie
                                    total_stock = stock_solaire + stock_reseau
                                    
                                    # --- LOGIQUE DE LISSAGE (PEAK SHAVING INTELLIGENT) ---
                                    # Si l'écrêtage est activé, on cherche à limiter le pic de soutirage
                                    if autoriser_ecretage_val:
                                        # --- LOGIQUE DE LISSAGE (50% Batterie / 50% Réseau) ---
                                        # Nouveau : On couvre 50% du besoin peu importe le seuil
                                        besoin_a_couvrir = besoin * 0.5
                                        decharge_totale = min(besoin_a_couvrir / RENDEMENT_DECHARGE, total_stock, p_batt_max_test)
                                    else:
                                        # Si on est sous le seuil, on décharge normalement pour maximiser l'autoconsommation
                                        decharge_totale = min(besoin / RENDEMENT_DECHARGE, total_stock, p_batt_max_test)
                                    
                                    if total_stock > 0:
                                        ratio_sol = stock_solaire / total_stock
                                        ratio_res = stock_reseau / total_stock
                                        stock_solaire -= decharge_totale * ratio_sol
                                        stock_reseau -= decharge_totale * ratio_res
                                    
                                    p_batt_real_max = max(p_batt_real_max, decharge_totale)
                                    auto_temp_kwh += decharge_totale * RENDEMENT_DECHARGE
                                    total_decharge_sim += decharge_totale * RENDEMENT_DECHARGE
                                    p_max_net = max(p_max_net, besoin - decharge_totale * RENDEMENT_DECHARGE)
                                    dech_sol = decharge_totale

                        # --- PRIORITÉ 2 : L'ARBITRAGE RÉSEAU ---
                        # On ne charge depuis le réseau que sur la puissance et capacité résiduelle
                        if (autoriser_arbitrage_val or autoriser_prix_dynamiques_val) and est_prix_bas and (stock_solaire + stock_reseau) < cap_utile_b:
                            # Puissance restante de l'onduleur
                            p_dispo_batt = p_batt_max_test - (charge_sol if ph >= ch else dech_sol)
                            if p_dispo_batt > 0:
                                charge_res = min(p_dispo_batt, (cap_utile_b - (stock_solaire + stock_reseau)) / RENDEMENT_CHARGE)
                                stock_reseau += charge_res * RENDEMENT_CHARGE
                                total_charge_reseau += charge_res
                                p_batt_real_max = max(p_batt_real_max, (charge_res + charge_sol) if ph >= ch else (charge_res + dech_sol))
                        
                        soc_points.append(stock_solaire + stock_reseau)
                    
                    cyclage_annuel = total_decharge_sim / cap_b if cap_b > 0 else 0
                    remplissage_moyen = (sum(soc_points) / len(soc_points)) / cap_b * 100 if (cap_b > 0 and len(soc_points) > 0) else 0
                    ratio_puissance = (p_batt_real_max / p_batt_max_test * 100) if p_batt_max_test > 0 else 0
                    
                    prod_annuelle_test = sum(prod_h_test)
                    t_prod = (auto_temp_kwh / conso_annuelle_kwh_val * 100) if conso_annuelle_kwh_val > 0 else 0
                    t_auto = (auto_temp_kwh / prod_annuelle_test * 100) if prod_annuelle_test > 0 else 0
                    surplus_test = max(0, prod_annuelle_test - auto_temp_kwh)
                    
                    # Valorisation financière
                    if "location" in scénario_investissement_val:
                        tarif_hp = prix_revente_locataire_val
                        gain_annuel_brut = (auto_temp_kwh * tarif_hp) + (surplus_test * prix_vente_val)
                    elif autoriser_prix_dynamiques_val:
                        # Gain = Somme des économies horaires
                        gain_annuel_brut = (surplus_test * prix_vente_val)
                        s_temp_sim_sol = 0.0
                        s_temp_sim_res = 0.0
                        cap_utile_sim = cap_b * DOD
                        p_batt_max_sim = cap_b * C_RATE
                        
                        # Surplus journalier attendu pour l'anticipation
                        surplus_journalier_sim = []
                        for j in range(365):
                            s_j = 0
                            for h in range(24):
                                idx_j = j*24 + h
                                s_j += max(0, prod_h_test[idx_j] - courbe_conso_val_calc[idx_j])
                            surplus_journalier_sim.append(s_j)
                        
                        for h_idx, (ph, ch) in enumerate(zip(prod_h_test, courbe_conso_val_calc)):
                            prix_h = vecteur_prix_achat[h_idx]
                            h_j = h_idx % 24
                            j_j = h_idx // 24
                            
                            # PRIORITÉ 1 : SOLAIRE
                            charge_s = 0
                            dech_s = 0
                            if ph >= ch:
                                gain_annuel_brut += ch * prix_h
                                dispo = ph - ch
                                charge_s = min(dispo, (cap_utile_sim - (s_temp_sim_sol + s_temp_sim_res)) / RENDEMENT_CHARGE, p_batt_max_sim)
                                s_temp_sim_sol += charge_s * RENDEMENT_CHARGE
                            else:
                                auto_h = ph
                                besoin = ch - ph
                                total_s_sim = s_temp_sim_sol + s_temp_sim_res
                                decharge_t = min(besoin / RENDEMENT_DECHARGE, total_s_sim, p_batt_max_sim)
                                if total_s_sim > 0:
                                    r_sol = s_temp_sim_sol / total_s_sim
                                    r_res = s_temp_sim_res / total_s_sim
                                    s_temp_sim_sol -= decharge_t * r_sol
                                    s_temp_sim_res -= decharge_t * r_res
                                dech_s = decharge_t
                                auto_h += decharge_t * RENDEMENT_DECHARGE
                                gain_annuel_brut += auto_h * prix_h

                            # PRIORITÉ 2 : ARBITRAGE
                            if (autoriser_arbitrage_val or autoriser_prix_dynamiques_val):
                                prix_moyen = sum(vecteur_prix_achat) / 8760
                                if prix_h < (prix_moyen * 0.8):
                                    est_prix_bas_sim = True
                                    if 0 <= h_j <= 6:
                                        place_r = max(0, cap_utile_sim - surplus_journalier_sim[j_j])
                                        if (s_temp_sim_sol + s_temp_sim_res) >= place_r:
                                            est_prix_bas_sim = False
                                    
                                    if est_prix_bas_sim:
                                        p_dispo = p_batt_max_sim - (charge_s if ph >= ch else dech_s)
                                        if p_dispo > 0:
                                            charge_r = min(p_dispo, (cap_utile_sim - (s_temp_sim_sol + s_temp_sim_res)) / RENDEMENT_CHARGE)
                                            s_temp_sim_res += charge_r * RENDEMENT_CHARGE
                                            gain_annuel_brut -= charge_r * prix_h
                    else:
                        tarif_hp = prix_achat_val
                        if autoriser_arbitrage_val or autoriser_prix_dynamiques_val:
                            # Gain arbitrage basé sur le coût évité ou le différentiel
                            economie_arbitrage = total_charge_reseau * (tarif_hp - (sum(vecteur_prix_achat)/8760))
                            gain_annuel_brut = (auto_temp_kwh * tarif_hp) + (surplus_test * prix_vente_val) + economie_arbitrage
                        else:
                            gain_annuel_brut = (auto_temp_kwh * tarif_hp) + (surplus_test * prix_vente_val)
                    
                    # Revenus additionnels batterie (Peak Shaving et Services Systèmes)
                    revenu_ecretage = 0
                    if autoriser_ecretage_val and cap_b > 0:
                        # On a déjà les infos pour calculer profil_net si besoin, mais on le refait par sécurité ou on optimise
                        profil_net = []
                        s_temp_sim = 0.0
                        cap_utile_sim = cap_b * DOD
                        p_batt_max_sim = cap_b * C_RATE
                        for ph, ch in zip(prod_h_test, courbe_conso_val_calc):
                            if ph >= ch:
                                dispo = ph - ch
                                charge = min(dispo, (cap_utile_sim - s_temp_sim) / RENDEMENT_CHARGE, p_batt_max_sim)
                                s_temp_sim += charge * RENDEMENT_CHARGE
                                profil_net.append(0) # Surplus ou zero
                            else:
                                besoin = ch - ph
                                decharge = min(besoin / RENDEMENT_DECHARGE, s_temp_sim, p_batt_max_sim)
                                s_temp_sim -= decharge
                                net = besoin - decharge * RENDEMENT_DECHARGE
                                profil_net.append(net)
                        
                        if st.session_state.get("pays_selectionne") == "France":
                            # En France, on calcule deux types de gains :
                            # 1. Gain avec l'abonnement ACTUEL (réduction des dépassements)
                            nb_h_dep_init = sum(1 for c_i in courbe_conso_val_calc if c_i > abonnement_val_val + 0.01)
                            nb_h_dep_final_actuel = sum(1 for p_n in profil_net if p_n > abonnement_val_val + 0.01)
                            gain_ecretage_actuel = max(0, nb_h_dep_init - nb_h_dep_final_actuel) * taxe_puissance_annuelle_val
                            
                            # 2. Gain avec l'abonnement OPTIMAL
                            best_gain_local = -float('inf')
                            best_abo_local = abonnement_val_val
                            depassements = [p_n for p_n in profil_net if p_n > 0.01]
                            
                            # Liste des paliers à tester
                            paliers = {3.0, abonnement_val_val}
                            if offre_france_val == "Tarif bleu particulier":
                                paliers.update([6, 9, 12, 15, 18, 24, 30, 36])
                            elif offre_france_val == "Tarif bleu pro":
                                paliers.update([3, 6, 9, 12, 15, 18, 24, 30, 36])
                            
                            if depassements:
                                for p in sorted(depassements, reverse=True)[:50]:
                                    val = math.ceil(p)
                                    if 3 <= val <= abonnement_val_val:
                                        paliers.add(float(val))
                            
                            for abo_t in paliers:
                                if abo_t > abonnement_val_val: continue
                                g_fixe = (abonnement_val_val - abo_t) * cout_abonnement_kva_val
                                nb_h_d = sum(1 for p_n in profil_net if p_n > abo_t + 0.01)
                                c_dep = nb_h_d * taxe_puissance_annuelle_val
                                if (g_fixe - c_dep) > best_gain_local:
                                    best_gain_local = g_fixe - c_dep
                                    best_abo_local = abo_t
                            
                            # On retient le meilleur des deux pour le ROI de l'optimiseur
                            revenu_ecretage = max(gain_ecretage_actuel, best_gain_local)
                        else:
                            # Suisse : Économie sur la taxe de puissance mensuelle
                            gain_ecretage_total = 0
                            jours_par_mois_calc = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
                            idx_h = 0
                            for m in range(12):
                                heures_mois = jours_par_mois_calc[m] * 24
                                # Max mensuel initial (sans système)
                                p_max_mensuel_initial = max(courbe_conso_val_calc[idx_h : idx_h + heures_mois]) if courbe_conso_val_calc[idx_h : idx_h + heures_mois] else 0
                                # Max mensuel net atteint avec le système
                                p_max_mensuel_net = max(profil_net[idx_h : idx_h + heures_mois]) if profil_net[idx_h : idx_h + heures_mois] else 0
                                
                                # L'économie se fait sur la réduction du pic effectif du bâtiment par rapport à son pic initial
                                reduction = max(0, p_max_mensuel_initial - p_max_mensuel_net)
                                gain_ecretage_total += reduction * taxe_puissance_annuelle_val
                                idx_h += heures_mois
                            revenu_ecretage = gain_ecretage_total

                    revenu_services = (cap_b / 1000) * revenu_services_unit_val if autoriser_services_val and cap_b > 0 else 0
                    gain_annuel_brut += revenu_ecretage + revenu_services

                    opex_annuel = (p_test * opex_pv_unit_val) + (cap_b * opex_batt_unit_val)
                    gain_annuel_net = gain_annuel_brut - opex_annuel
                    capex_test = (p_test * capex_pv_unit_val) + (cap_b * capex_batt_unit_val)
                    van_test = (gain_annuel_net * duree_projet_val) - capex_test
                    
                    best_pv_total = p_test
                    best_capa_batt = cap_b
                    best_gain_annuel = gain_annuel_net
                    best_capex = capex_test
                    best_taux_auto_config = t_auto
                    best_taux_prod_config = t_prod
                    best_surplus_config = surplus_test
                    best_economies = van_test
                    
                    scenarios_comparaison.append({
                        "Label": "Configuration Manuelle",
                        "Autoproduction": t_prod,
                        "Autoconsommation": t_auto,
                        "ROI": round(capex_test / gain_annuel_net, 1) if gain_annuel_net > 0 else 99,
                        "Economies": round(van_test),
                        "Cyclage": round(cyclage_annuel),
                        "Remplissage": round(remplissage_moyen),
                        "RatioPuissance": round(ratio_puissance)
                    })
                else:
                    # MODE AUTO : Recherche de l'optimum
                    # On définit des paliers de test pour la puissance PV
                    p_totale_max_toit = sum(p['p_max'] for p in profils_unitaires_par_pan)
                    
                    # Nouvelle Logique PV selon les instructions
                    p_base = conso_annuelle_kwh_val / productible_moyen if productible_moyen > 0 else 20.0
                    
                    if "autonomie" in mode_ideal_val.lower():
                        p_start = p_base        # 100% de la conso
                        p_max_test = p_base * 2.0 # 200% de la conso
                    else: # Favoriser le ROI (aspect financier)
                        p_start = p_base * 0.5  # 50% de la conso
                        p_max_test = p_base * 2.0 # 200% de la conso
                    
                    paliers_pv = []
                    # On teste par paliers de 10% de la puissance de base (p_base)
                    curr_p = p_start
                    while curr_p <= p_max_test + 0.001:
                        if curr_p <= p_totale_max_toit:
                            paliers_pv.append(curr_p)
                        curr_p += p_base * 0.1
                    
                    if not paliers_pv:
                        paliers_pv = [min(p_start, p_totale_max_toit)]
                    
                    paliers_pv = sorted(list(set(paliers_pv)))
                    
                    with st.spinner("Calcul du dimensionnement idéal..."):
                        for p_test in paliers_pv:
                            ratio_pv = p_test / p_totale_max_toit if p_totale_max_toit > 0 else 0
                            prod_h_test = [0.0] * 8760
                            for item in profils_unitaires_par_pan:
                                p_pan_test = item['p_max'] * ratio_pv
                                for i in range(8760):
                                    prod_h_test[i] += item['profil'][i] * p_pan_test
                            
                            # Logique Batterie : de 20 kWh à 2x puissance PV
                            b_start = 20.0
                            b_max = p_test * 2.0
                            
                            if not simuler_batterie_val:
                                paliers_batt = [0.0]
                            elif b_max < b_start:
                                paliers_batt = [b_start]
                            else:
                                # On teste par paliers de 20% du max batterie (ou au moins 5 paliers)
                                step_b = max(10.0, (b_max - b_start) / 5)
                                paliers_batt = []
                                curr_b = b_start
                                while curr_b <= b_max + 0.001:
                                    paliers_batt.append(curr_b)
                                    curr_b += step_b
                            
                            paliers_batt = sorted(list(set(paliers_batt)), reverse=True)
                            
                            for cap_b in paliers_batt:
                                cap_utile_b = cap_b * DOD
                                s_temp = 0.0
                                auto_temp_kwh = 0
                                p_batt_max_test = cap_b * C_RATE
                                soc_points = []
                                p_max_net = 0
                                total_decharge_sim = 0
                                p_batt_real_max = 0
                                total_charge_reseau = 0
                                
                                # Pour suivre l'origine de l'énergie dans la batterie (Solaire vs Réseau)
                                stock_solaire = 0.0 # Énergie stockée issue du PV (kWh)
                                stock_reseau = 0.0  # Énergie stockée issue du réseau (kWh)
                                
                                # Seuil pour l'arbitrage dynamique
                                if autoriser_prix_dynamiques_val:
                                    prix_moyen = sum(vecteur_prix_achat) / 8760
                                    seuil_charge = prix_moyen * 0.8
                                
                                # --- MODIFICATION : On ignore l'arbitrage/écrêtage/services pour l'optimisation du système idéal ---
                                # On force ces flags à False localement pour la boucle de recherche SI l'option n'est pas activée
                                if optimiser_avec_options_val:
                                    local_autoriser_arbitrage = autoriser_arbitrage_val
                                    local_autoriser_prix_dynamiques = autoriser_prix_dynamiques_val
                                    local_autoriser_ecretage = autoriser_ecretage_val
                                    local_autoriser_services = autoriser_services_val
                                else:
                                    local_autoriser_arbitrage = False
                                    local_autoriser_prix_dynamiques = False
                                    local_autoriser_ecretage = False
                                    local_autoriser_services = False

                                # Seuil pour l'arbitrage dynamique
                                if autoriser_prix_dynamiques_val:
                                    prix_moyen = sum(vecteur_prix_achat) / 8760
                                    seuil_charge = prix_moyen * 0.8
                                
                                # Calcul du surplus solaire attendu pour chaque jour
                                surplus_journalier_attendu_o = []
                                for j in range(365):
                                    s_jour = 0
                                    for h in range(24):
                                        idx = j*24 + h
                                        s_jour += max(0, prod_h_test[idx] - courbe_conso_val_calc[idx])
                                    surplus_journalier_attendu_o.append(s_jour)

                                for h_idx, (ph, ch) in enumerate(zip(prod_h_test, courbe_conso_val_calc)):
                                    h_jour = h_idx % 24
                                    j_idx = h_idx // 24
                                    prix_h = vecteur_prix_achat[h_idx]
                                    
                                    # Détermination si prix bas pour arbitrage Spot
                                    est_prix_bas = False
                                    if (local_autoriser_arbitrage or local_autoriser_prix_dynamiques):
                                        # Seuil de rentabilité : le prix doit être inférieur à 80% du prix moyen 
                                        est_prix_bas = prix_h < (seuil_charge)
                                        
                                    # Anticipation solaire STRICTE
                                    if est_prix_bas and 0 <= h_jour <= 6:
                                        surplus_prevu = surplus_journalier_attendu_o[j_idx]
                                        place_pour_reseau = max(0, cap_utile_b - surplus_prevu)
                                        if (stock_solaire + stock_reseau) >= place_pour_reseau:
                                            est_prix_bas = False

                                    # --- PRIORITÉ 1 : LE SOLAIRE ---
                                    charge_sol = 0
                                    dech_sol = 0
                                    if ph >= ch:
                                        # Consommation directe du solaire
                                        auto_temp_kwh += ch
                                        dispo_solaire = ph - ch
                                    
                                        # Charger la batterie avec le surplus solaire
                                        charge_sol = min(dispo_solaire, (cap_utile_b - (stock_solaire + stock_reseau)) / RENDEMENT_CHARGE, p_batt_max_test)
                                        stock_solaire += charge_sol * RENDEMENT_CHARGE
                                        p_batt_real_max = max(p_batt_real_max, charge_sol)
                                        p_max_net = max(p_max_net, 0)
                                    else:
                                        # Solaire insuffisant
                                        auto_temp_kwh += ph
                                        besoin = ch - ph
                                    
                                        # Décharger la batterie pour couvrir le besoin
                                        total_stock = stock_solaire + stock_reseau
                                        
                                        # --- LOGIQUE DE LISSAGE (PEAK SHAVING INTELLIGENT) ---
                                        if local_autoriser_ecretage:
                                            # --- LOGIQUE DE LISSAGE (50% Batterie / 50% Réseau) ---
                                            # Nouveau : On couvre 50% du besoin peu importe le seuil
                                            besoin_a_couvrir = besoin * 0.5
                                            decharge_totale = min(besoin_a_couvrir / RENDEMENT_DECHARGE, total_stock, p_batt_max_test)
                                        else:
                                            decharge_totale = min(besoin / RENDEMENT_DECHARGE, total_stock, p_batt_max_test)
                                        
                                        if total_stock > 0:
                                            ratio_sol = stock_solaire / total_stock
                                            ratio_res = stock_reseau / total_stock
                                            stock_solaire -= decharge_totale * ratio_sol
                                            stock_reseau -= decharge_totale * ratio_res
                                        
                                        p_batt_real_max = max(p_batt_real_max, decharge_totale)
                                        auto_temp_kwh += decharge_totale * RENDEMENT_DECHARGE
                                        total_decharge_sim += decharge_totale * RENDEMENT_DECHARGE
                                        p_max_net = max(p_max_net, besoin - decharge_totale * RENDEMENT_DECHARGE)
                                        dech_sol = decharge_totale

                                    # --- PRIORITÉ 2 : L'ARBITRAGE RÉSEAU ---
                                    if (local_autoriser_arbitrage or local_autoriser_prix_dynamiques) and est_prix_bas and (stock_solaire + stock_reseau) < cap_utile_b:
                                        p_dispo_batt = p_batt_max_test - (charge_sol if ph >= ch else dech_sol)
                                        if p_dispo_batt > 0:
                                            charge_res = min(p_dispo_batt, (cap_utile_b - (stock_solaire + stock_reseau)) / RENDEMENT_CHARGE)
                                            stock_reseau += charge_res * RENDEMENT_CHARGE
                                            total_charge_reseau += charge_res
                                            p_batt_real_max = max(p_batt_real_max, (charge_res + charge_sol) if ph >= ch else (charge_res + dech_sol))

                                    soc_points.append(stock_solaire + stock_reseau)

                                cyclage_annuel = total_decharge_sim / cap_b if cap_b > 0 else 0
                                remplissage_moyen = (sum(soc_points) / len(soc_points)) / cap_b * 100 if (cap_b > 0 and len(soc_points) > 0) else 0
                                ratio_puissance = (p_batt_real_max / p_batt_max_test * 100) if p_batt_max_test > 0 else 0

                                prod_annuelle_test = sum(prod_h_test)
                                t_prod = (auto_temp_kwh / conso_annuelle_kwh_val * 100) if conso_annuelle_kwh_val > 0 else 0
                                t_auto = (auto_temp_kwh / prod_annuelle_test * 100) if prod_annuelle_test > 0 else 0
                                surplus_test = max(0, prod_annuelle_test - auto_temp_kwh)
                                
                                if "location" in scénario_investissement_val:
                                    tarif_hp = prix_revente_locataire_val
                                    gain_annuel_brut = (auto_temp_kwh * tarif_hp) + (surplus_test * prix_vente_val)
                                elif local_autoriser_prix_dynamiques:
                                    gain_annuel_brut = (surplus_test * prix_vente_val)
                                    s_temp_sim_sol = 0.0
                                    s_temp_sim_res = 0.0
                                    cap_utile_sim = cap_b * DOD
                                    p_batt_max_sim = cap_b * C_RATE
                                    
                                    # Surplus journalier attendu pour l'anticipation
                                    surplus_journalier_sim = []
                                    for j in range(365):
                                        s_j = 0
                                        for h in range(24):
                                            idx_j = j*24 + h
                                            s_j += max(0, prod_h_test[idx_j] - courbe_conso_val_calc[idx_j])
                                        surplus_journalier_sim.append(s_j)
                                    
                                    for h_idx, (ph, ch) in enumerate(zip(prod_h_test, courbe_conso_val_calc)):
                                        prix_h = vecteur_prix_achat[h_idx]
                                        h_j = h_idx % 24
                                        j_j = h_idx // 24
                                        
                                        # PRIORITÉ 1 : SOLAIRE
                                        charge_s = 0
                                        dech_s = 0
                                        if ph >= ch:
                                            gain_annuel_brut += ch * prix_h
                                            dispo = ph - ch
                                            charge_s = min(dispo, (cap_utile_sim - (s_temp_sim_sol + s_temp_sim_res)) / RENDEMENT_CHARGE, p_batt_max_sim)
                                            s_temp_sim_sol += charge_s * RENDEMENT_CHARGE
                                        else:
                                            auto_h = ph
                                            besoin = ch - ph
                                            total_s_sim = s_temp_sim_sol + s_temp_sim_res
                                            decharge_t = min(besoin / RENDEMENT_DECHARGE, total_s_sim, p_batt_max_sim)
                                            if total_s_sim > 0:
                                                r_sol = s_temp_sim_sol / total_s_sim
                                                r_res = s_temp_sim_res / total_s_sim
                                                s_temp_sim_sol -= decharge_t * r_sol
                                                s_temp_sim_res -= decharge_t * r_res
                                            dech_s = decharge_t
                                            auto_h += decharge_t * RENDEMENT_DECHARGE
                                            gain_annuel_brut += auto_h * prix_h

                                        # PRIORITÉ 2 : ARBITRAGE
                                        if (local_autoriser_arbitrage or local_autoriser_prix_dynamiques):
                                            prix_moyen = sum(vecteur_prix_achat) / 8760
                                            if prix_h < (prix_moyen * 0.8):
                                                est_prix_bas_sim = True
                                                if 0 <= h_j <= 6:
                                                    place_r = max(0, cap_utile_sim - surplus_journalier_sim[j_j])
                                                    if (s_temp_sim_sol + s_temp_sim_res) >= place_r:
                                                        est_prix_bas_sim = False
                                                
                                                if est_prix_bas_sim:
                                                    p_dispo = p_batt_max_sim - (charge_s if ph >= ch else dech_s)
                                                    if p_dispo > 0:
                                                        charge_r = min(p_dispo, (cap_utile_sim - (s_temp_sim_sol + s_temp_sim_res)) / RENDEMENT_CHARGE)
                                                        s_temp_sim_res += charge_r * RENDEMENT_CHARGE
                                                        gain_annuel_brut -= charge_r * prix_h
                                else:
                                   tarif_hp = prix_achat_val
                                   if local_autoriser_arbitrage or local_autoriser_prix_dynamiques:
                                       economie_arbitrage = total_charge_reseau * (tarif_hp - (sum(vecteur_prix_achat)/8760))
                                       gain_annuel_brut = (auto_temp_kwh * tarif_hp) + (surplus_test * prix_vente_val) + economie_arbitrage
                                   else:
                                       gain_annuel_brut = (auto_temp_kwh * tarif_hp) + (surplus_test * prix_vente_val)
                                
                                # Revenus additionnels batterie (Peak Shaving et Services Systèmes)
                                revenu_ecretage = 0
                                if local_autoriser_ecretage and cap_b > 0:
                                    # Calcul mensuel de la réduction de puissance max
                                    gain_ecretage_total = 0
                                    jours_par_mois_calc = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
                                    
                                    # On simule le profil net heure par heure pour toute l'année
                                    profil_net = []
                                    s_temp_sim = 0.0
                                    cap_utile_sim = cap_b * DOD
                                    p_batt_max_sim = cap_b * C_RATE
                                    for ph, ch in zip(prod_h_test, courbe_conso_val_calc):
                                        if ph >= ch:
                                            dispo = ph - ch
                                            charge = min(dispo, (cap_utile_sim - s_temp_sim) / RENDEMENT_CHARGE, p_batt_max_sim)
                                            s_temp_sim += charge * RENDEMENT_CHARGE
                                            profil_net.append(0)
                                        else:
                                            besoin = ch - ph
                                            decharge = min(besoin / RENDEMENT_DECHARGE, s_temp_sim, p_batt_max_sim)
                                            s_temp_sim -= decharge
                                            net = besoin - decharge * RENDEMENT_DECHARGE
                                            profil_net.append(net)
                                    
                                    idx_h = 0
                                    for m in range(12):
                                        heures_mois = jours_par_mois_calc[m] * 24
                                        # Max mensuel initial
                                        p_max_mensuel_initial = max(courbe_conso_val_calc[idx_h : idx_h + heures_mois]) if courbe_conso_val_calc[idx_h : idx_h + heures_mois] else 0
                                        # Max mensuel net
                                        p_max_mensuel_net = max(profil_net[idx_h : idx_h + heures_mois]) if profil_net[idx_h : idx_h + heures_mois] else 0
                                        
                                        if st.session_state.get("pays_selectionne") == "France":
                                            # En France, on calcule deux types de gains :
                                            # 1. Gain avec l'abonnement ACTUEL (réduction des dépassements)
                                            nb_h_dep_init = sum(1 for c_i in courbe_conso_val_calc if c_i > abonnement_val_val + 0.01)
                                            nb_h_dep_final_actuel = sum(1 for p_n in profil_net if p_n > abonnement_val_val + 0.01)
                                            gain_ecretage_actuel = max(0, nb_h_dep_init - nb_h_dep_final_actuel) * taxe_puissance_annuelle_val
                                            
                                            # 2. Gain avec l'abonnement OPTIMAL
                                            best_gain_local = -float('inf')
                                            best_abo_local = abonnement_val_val
                                            depassements = [p_n for p_n in profil_net if p_n > 0.01]
                                            
                                            # Liste des paliers à tester
                                            paliers = {3.0, abonnement_val_val}
                                            if offre_france_val == "Tarif bleu particulier":
                                                paliers.update([6, 9, 12, 15, 18, 24, 30, 36])
                                            elif offre_france_val == "Tarif bleu pro":
                                                paliers.update([3, 6, 9, 12, 15, 18, 24, 30, 36])
                                            
                                            if depassements:
                                                for p in sorted(depassements, reverse=True)[:50]:
                                                    val = math.ceil(p)
                                                    if 3 <= val <= abonnement_val_val:
                                                        paliers.add(float(val))
                                            
                                            for abo_t in paliers:
                                                if abo_t > abonnement_val_val: continue
                                                g_fixe = (abonnement_val_val - abo_t) * cout_abonnement_kva_val
                                                nb_h_d = sum(1 for p_n in profil_net if p_n > abo_t + 0.01)
                                                c_dep = nb_h_d * taxe_puissance_annuelle_val
                                                if (g_fixe - c_dep) > best_gain_local:
                                                    best_gain_local = g_fixe - c_dep
                                                    best_abo_local = abo_t
                                            
                                            # On retient le meilleur des deux pour le ROI de l'optimiseur
                                            revenu_ecretage = max(gain_ecretage_actuel, best_gain_local)
                                            # Stockage pour affichage final
                                            if p_test == best_pv_total and cap_b == best_capa_batt:
                                                gain_ecretage_actuel_final = gain_ecretage_actuel
                                                gain_ecretage_optimal_final = best_gain_local
                                                best_abo_optimal_final = best_abo_local
                                        else:
                                            # Suisse : réduction du pic mensuel réel
                                            reduction = max(0, p_max_mensuel_initial - p_max_mensuel_net)
                                            gain_ecretage_total += reduction * taxe_puissance_annuelle_val
                                        idx_h += heures_mois
                                    revenu_ecretage = gain_ecretage_total

                                revenu_services = (cap_b / 1000) * revenu_services_unit_val if local_autoriser_services and cap_b > 0 else 0
                                gain_annuel_brut += revenu_ecretage + revenu_services

                                opex_annuel = (p_test * opex_pv_unit_val) + (cap_b * opex_batt_unit_val)
                                gain_annuel_net = gain_annuel_brut - opex_annuel
                                capex_test = (p_test * capex_pv_unit_val) + (cap_b * capex_batt_unit_val)
                                
                                gain_cumule = gain_annuel_net * duree_projet_val
                                van_test = gain_cumule - capex_test
                                roi_test = capex_test / gain_annuel_net if gain_annuel_net > 0 else 99
                                
                                scenarios_comparaison.append({
                                    "Label": f"{p_test:,.1f} kWc / {int(cap_b)} kWh".replace(",", " "),
                                    "Autoproduction": t_prod,
                                    "Autoconsommation": t_auto,
                                    "ROI": round(roi_test, 1),
                                    "Economies": round(van_test),
                                    "Cyclage": round(cyclage_annuel),
                                    "Remplissage": round(remplissage_moyen),
                                    "RatioPuissance": round(ratio_puissance)
                                })

                                if "autonomie" in mode_ideal_val.lower():
                                    # Favoriser l'autonomie sur site : taux d'autoproduction le plus haut avec le + d'économie
                                    # On vérifie d'abord si la batterie respecte les critères de sollicitation (si batterie il y a)
                                    respecte_criteres_batt = True
                                    if cap_b > 0:
                                        if cyclage_annuel < 150 or not (60 <= ratio_puissance <= 80) or not (40 <= remplissage_moyen <= 60):
                                            respecte_criteres_batt = False
                                    
                                    if respecte_criteres_batt:
                                        # Score basé sur l'autoproduction, avec un petit bonus pour les économies (VAN)
                                        # On divise par 10M pour que le VAN ne l'emporte jamais sur 1% d'autoproduction
                                        score = t_prod + (van_test / 10_000_000)
                                    else:
                                        # Si critères non respectés, on pénalise mais on garde le classement relatif
                                        score = -1000 + t_prod
                                else:
                                    # Favoriser l'investissement : 
                                    # 1. ROI < 7.5 ans (Priorité absolue)
                                    # 2. Maximum d'économies (VAN)
                                    # 3. Critères batterie (Bonus tie-breaker)
                                    
                                    respecte_criteres_batt = True
                                    if cap_b > 0:
                                        if cyclage_annuel < 150 or not (60 <= ratio_puissance <= 80) or not (40 <= remplissage_moyen <= 60):
                                            respecte_criteres_batt = False
                                    
                                    # Bonus critères batterie : on ajoute un bonus de 1 unité monétaire (négligeable face au VAN)
                                    # pour favoriser le système avec une batterie saine à VAN quasi égal.
                                    bonus_batt = 1.0 if respecte_criteres_batt else 0.0
                                    
                                    if roi_test <= 7.5:
                                        # Priorité 1 OK : On score sur le VAN (Priorité 2) + bonus batterie (Priorité 3)
                                        score = van_test + bonus_batt
                                    else:
                                        # ROI > 7.5 : On pénalise lourdement en fonction de l'écart au ROI cible
                                        # pour s'assurer que n'importe quel système ROI <= 7.5 gagne.
                                        score = -1_000_000_000 - (roi_test * 1_000_000) + van_test / 1000

                                if score > best_autoprod_score:
                                    best_autoprod_score = score
                                    best_pv_total = p_test
                                    best_capa_batt = cap_b
                                    # --- MODIFICATION : On recalculera les performances finales APRÈS avoir trouvé le système idéal ---
                                    # Pour inclure les options facultatives (arbitrage, etc.) sans qu'elles n'influencent le choix.
                        
                        # --- RECALCUL DES PERFORMANCES DU SYSTÈME IDÉAL AVEC LES OPTIONS FACULTATIVES ---
                        # Maintenant qu'on a le best_pv_total et best_capa_batt, on relance une simulation complète
                        # avec les vrais flags utilisateur (autoriser_arbitrage_val, etc.)
                        
                        ratio_pv_ideal = best_pv_total / p_totale_max_toit if p_totale_max_toit > 0 else 0
                        prod_h_ideal = [0.0] * 8760
                        for item in profils_unitaires_par_pan:
                            p_pan_ideal = item['p_max'] * ratio_pv_ideal
                            for i in range(8760):
                                prod_h_ideal[i] += item['profil'][i] * p_pan_ideal
                        
                        s_temp = 0.0
                        auto_temp_kwh = 0
                        cap_utile_b = best_capa_batt * DOD
                        p_batt_max_test = best_capa_batt * C_RATE
                        soc_points = []
                        total_decharge_sim = 0
                        p_batt_real_max = 0
                        total_charge_reseau = 0
                        
                        # Suivi des gains détaillés
                        gain_autoconsommation = 0
                        gain_vente_surplus = 0
                        gain_arbitrage = 0
                        
                        # Pour suivre l'origine de l'énergie dans la batterie (Solaire vs Réseau)
                        stock_solaire = 0.0 # Énergie stockée issue du PV (kWh)
                        stock_reseau = 0.0  # Énergie stockée issue du réseau (kWh)
                        
                        # Calcul du surplus solaire attendu pour chaque jour (pour l'anticipation)
                        surplus_journalier_attendu = []
                        for j in range(365):
                            s_jour = 0
                            for h in range(24):
                                idx = j*24 + h
                                s_jour += max(0, prod_h_ideal[idx] - courbe_conso_val_calc[idx])
                            surplus_journalier_attendu.append(s_jour)
                        
                        # Seuil pour l'arbitrage dynamique
                        if autoriser_prix_dynamiques_val:
                            prix_moyen = sum(vecteur_prix_achat) / 8760
                            seuil_charge = prix_moyen * 0.8
                        
                        for h_idx, (ph, ch) in enumerate(zip(prod_h_ideal, courbe_conso_val_calc)):
                            h_jour = h_idx % 24
                            j_idx = h_idx // 24
                            prix_h = vecteur_prix_achat[h_idx]
                            
                            est_prix_bas = False
                            if (autoriser_arbitrage_val or autoriser_prix_dynamiques_val):
                                est_prix_bas = prix_h < (seuil_charge)
                                
                                # Anticipation solaire STRICTE
                                if est_prix_bas and 0 <= h_jour <= 6:
                                    surplus_prevu = surplus_journalier_attendu[j_idx]
                                    place_pour_reseau = max(0, cap_utile_b - surplus_prevu)
                                    if (stock_solaire + stock_reseau) >= place_pour_reseau:
                                        est_prix_bas = False

                            # --- PRIORITÉ 1 : LE SOLAIRE ---
                            charge_sol = 0
                            dech_sol = 0
                            if ph >= ch:
                                # Consommation directe
                                auto_temp_kwh += ch
                                if "location" in scénario_investissement_val:
                                    gain_autoconsommation += ch * prix_revente_locataire_val
                                else:
                                    gain_autoconsommation += ch * prix_h
                                
                                dispo_solaire = ph - ch
                                # Charge batterie solaire
                                charge_sol = min(dispo_solaire, (cap_utile_b - (stock_solaire + stock_reseau)) / RENDEMENT_CHARGE, p_batt_max_test)
                                stock_solaire += charge_sol * RENDEMENT_CHARGE
                                p_batt_real_max = max(p_batt_real_max, charge_sol)
                            
                                # Surplus vendu
                                surplus_h = dispo_solaire - charge_sol
                                gain_vente_surplus += surplus_h * prix_vente_val
                            else:
                                # Solaire insuffisant
                                auto_temp_kwh += ph
                                if "location" in scénario_investissement_val:
                                    gain_autoconsommation += ph * prix_revente_locataire_val
                                else:
                                    gain_autoconsommation += ph * prix_h
                                
                                besoin = ch - ph
                                
                                # Décharge batterie
                                total_stock = stock_solaire + stock_reseau
                                
                                # --- LOGIQUE DE LISSAGE (PEAK SHAVING INTELLIGENT) ---
                                if autoriser_ecretage_val:
                                    # --- LOGIQUE DE LISSAGE (50% Batterie / 50% Réseau) ---
                                    # Nouveau : On couvre 50% du besoin peu importe le seuil
                                    besoin_a_couvrir = besoin * 0.5
                                    decharge_totale = min(besoin_a_couvrir / RENDEMENT_DECHARGE, total_stock, p_batt_max_test)
                                else:
                                    decharge_totale = min(besoin / RENDEMENT_DECHARGE, total_stock, p_batt_max_test)
                                
                                # Proportion de décharge selon l'origine du stock
                                if total_stock > 0:
                                    ratio_sol = stock_solaire / total_stock
                                    ratio_res = stock_reseau / total_stock
                                
                                    dech_sol_val = decharge_totale * ratio_sol
                                    dech_res_val = decharge_totale * ratio_res
                                
                                    stock_solaire -= dech_sol_val
                                    stock_reseau -= dech_res_val
                                
                                    # Valorisation
                                    if "location" in scénario_investissement_val:
                                        gain_autoconsommation += dech_sol_val * RENDEMENT_DECHARGE * prix_revente_locataire_val
                                        gain_arbitrage += dech_res_val * RENDEMENT_DECHARGE * prix_revente_locataire_val
                                    else:
                                        gain_autoconsommation += dech_sol_val * RENDEMENT_DECHARGE * prix_h
                                        gain_arbitrage += dech_res_val * RENDEMENT_DECHARGE * prix_h
                            
                                p_batt_real_max = max(p_batt_real_max, decharge_totale)
                                auto_temp_kwh += decharge_totale * RENDEMENT_DECHARGE
                                total_decharge_sim += decharge_totale * RENDEMENT_DECHARGE
                                dech_sol = decharge_totale

                            # --- PRIORITÉ 2 : L'ARBITRAGE RÉSEAU ---
                            if (autoriser_arbitrage_val or autoriser_prix_dynamiques_val) and est_prix_bas and (stock_solaire + stock_reseau) < cap_utile_b:
                                p_dispo_batt = p_batt_max_test - (charge_sol if ph >= ch else dech_sol)
                                if p_dispo_batt > 0:
                                    charge_res = min(p_dispo_batt, (cap_utile_b - (stock_solaire + stock_reseau)) / RENDEMENT_CHARGE)
                                    stock_reseau += charge_res * RENDEMENT_CHARGE
                                    total_charge_reseau += charge_res
                                    gain_arbitrage -= charge_res * prix_h
                                    p_batt_real_max = max(p_batt_real_max, (charge_res + charge_sol) if ph >= ch else (charge_res + dech_sol))
                        
                            soc_points.append(stock_solaire + stock_reseau)
                        
                        best_taux_prod_config = (auto_temp_kwh / conso_annuelle_kwh_val * 100) if conso_annuelle_kwh_val > 0 else 0
                        best_taux_auto_config = (auto_temp_kwh / sum(prod_h_ideal) * 100) if sum(prod_h_ideal) > 0 else 0
                        best_surplus_config = max(0, sum(prod_h_ideal) - auto_temp_kwh)
                        
                        # Revenus additionnels batterie (Peak Shaving et Services Systèmes)
                        revenu_ecretage = 0
                        if autoriser_ecretage_val and best_capa_batt > 0:
                            profil_net = []
                            s_temp_sim = 0.0
                            for ph, ch in zip(prod_h_ideal, courbe_conso_val_calc):
                                if ph >= ch:
                                    dispo = ph - ch
                                    charge = min(dispo, (cap_utile_b - s_temp_sim) / RENDEMENT_CHARGE, p_batt_max_test)
                                    s_temp_sim += charge * RENDEMENT_CHARGE
                                    profil_net.append(0)
                                else:
                                    besoin = ch - ph
                                    decharge = min(besoin / RENDEMENT_DECHARGE, s_temp_sim, p_batt_max_test)
                                    s_temp_sim -= decharge
                                    profil_net.append(besoin - decharge * RENDEMENT_DECHARGE)
                            
                            idx_h = 0
                            jours_m = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
                            gain_ecretage_total = 0 
                            for m in range(12):
                                hs = jours_m[m] * 24
                                if st.session_state.get("pays_selectionne") == "France":
                                    # --- VERSION FRANCE : OPTIMISATION AUTOMATIQUE ---
                                    # 1. Gain sur l'abonnement actuel (Dépassements évités)
                                    seuil_initial = abonnement_val_val
                                    nb_h_dep_init_mensuel = sum(1 for c_i in courbe_conso_val_calc[idx_h : idx_h + hs] if c_i > abonnement_val_val + 0.01)
                                    nb_h_dep_final_actuel_mensuel = sum(1 for p_n in profil_net[idx_h : idx_h + hs] if p_n > seuil_initial + 0.01)
                                    gain_ecretage_actuel = max(0, nb_h_dep_init_mensuel - nb_h_dep_final_actuel_mensuel) * taxe_puissance_annuelle_val
                                    
                                    # 2. Recherche du meilleur abonnement possible (Gain optimal)
                                    best_gain_local = -999999999
                                    best_abo_local = abonnement_val_val
                                    depassements = [p_n for p_n in profil_net if p_n > 0.01]
                                    
                                    # Paliers standards
                                    paliers = {3.0, abonnement_val_val}
                                    if 'offre_france_val' in locals() and offre_france_val == "Tarif bleu particulier":
                                        paliers.update([6, 9, 12, 15, 18, 24, 30, 36])
                                    elif 'offre_france_val' in locals() and offre_france_val == "Tarif bleu pro":
                                        paliers.update([3, 6, 9, 12, 15, 18, 24, 30, 36])

                                    if depassements:
                                        for p in sorted(depassements, reverse=True)[:50]:
                                            val = math.ceil(p)
                                            if 3 <= val <= abonnement_val_val:
                                                paliers.add(float(val))
                                    
                                    for abo_t in paliers:
                                        if abo_t > abonnement_val_val: continue
                                        g_fixe = (abonnement_val_val - abo_t) * cout_abonnement_kva_val
                                        nb_h_d_local = sum(1 for p_n in profil_net[idx_h : idx_h + hs] if p_n > abo_t + 0.01)
                                        c_dep_local = nb_h_d_local * taxe_puissance_annuelle_val
                                        if (g_fixe/12 - c_dep_local) > best_gain_local:
                                            best_gain_local = g_fixe/12 - c_dep_local
                                            best_abo_local = abo_t
                                    
                                    gain_ecretage_optimal = best_gain_local
                                    abo_optimal_local = best_abo_local
                                    
                                    # On retient le gain optimal pour la rentabilité globale (VAN)
                                    gain_ecretage_total += gain_ecretage_optimal
                                    
                                    # Initialisation des accumulateurs pour l'affichage final
                                    if m == 0:
                                        total_gain_actuel = 0
                                        total_gain_optimal = 0
                                        meilleur_abo_final = abonnement_val_val
                                    
                                    total_gain_actuel += gain_ecretage_actuel
                                    total_gain_optimal += gain_ecretage_optimal
                                    meilleur_abo_final = min(meilleur_abo_final, abo_optimal_local)

                                    # Pour compatibilité avec les variables d'affichage existantes
                                    nouvel_abo_propose = meilleur_abo_final
                                    gain_actuel_details = total_gain_actuel
                                    gain_optimal_details = total_gain_optimal
                                    
                                    # Correction : revenu_ecretage doit être total_gain_optimal (annuel)
                                    revenu_ecretage = total_gain_optimal
                                else:
                                    # Suisse : réduction du pic mensuel réel
                                    p_init = max(courbe_conso_val_calc[idx_h:idx_h+hs])
                                    p_net = max(profil_net[idx_h:idx_h+hs])
                                    gain_ecretage_total += max(0, p_init - p_net) * taxe_puissance_annuelle_val
                                    revenu_ecretage = gain_ecretage_total
                                
                                idx_h += hs
                        
                        revenu_services = (best_capa_batt / 1000) * revenu_services_unit_val if autoriser_services_val and best_capa_batt > 0 else 0
                        
                        best_gain_annuel = gain_autoconsommation + gain_vente_surplus + gain_arbitrage + revenu_ecretage + revenu_services - (best_pv_total * opex_pv_unit_val) - (best_capa_batt * opex_batt_unit_val)
                        best_capex = (best_pv_total * capex_pv_unit_val) + (best_capa_batt * capex_batt_unit_val)
                        best_economies = (best_gain_annuel * duree_projet_val) - best_capex
            
            aug_intro_ideale = max(0.0, best_pv_total - puissance_intro_kw_val)
            
            # --- AFFICHAGE MÉTRIQUES IDÉALES ---
            c_id1, c_id2, c_id3 = st.columns(3)
            c_id1.metric("Puissance PV Idéale", f"{best_pv_total:,.1f} kWc".replace(",", " "), help="Puissance PV optimisant le compromis entre autonomie et rentabilité.")
            # Calcul de la puissance du stockage
            if simuler_batterie_val:
                puissance_stockage = best_capa_batt * C_RATE
                c_id2.metric("Stockage Idéal", f"{int(puissance_stockage):,} kW/{int(best_capa_batt):,} kWh".replace(",", " "), help="Puissance et capacité de stockage optimisées.")
            else:
                c_id2.metric("Stockage Idéal", "Aucun", help="La simulation de batterie est désactivée.")
            
            if aug_intro_ideale > 0:
                if unite_intro_val == "kVA":
                    label_aug = f"+{aug_intro_ideale:,.1f} kVA".replace(",", " ")
                else:
                    amp_aug = (aug_intro_ideale * 1000) / (400 * 1.732)
                    label_aug = f"+{int(amp_aug):,} A".replace(",", " ")
                c_id3.metric("Augmentation d'intro", label_aug, delta=f"Besoin de {best_pv_total:,.1f} kW au total".replace(",", " "), delta_color="inverse")
            else:
                c_id3.metric("Augmentation d'intro", "Aucune")

            # --- NOUVELLE LIGNE DE PERFORMANCE IDÉALE ---
            st.write("#### ⚡ Performance du système idéal")
            
            # Calcul des gains par rapport à la section 2 (PV seul au max du toit)
            gain_auto_prod = best_taux_prod_config - taux_autoproduction
            gain_auto_conso = best_taux_auto_config - taux_autoconsommation
            diff_surplus = best_surplus_config - surplus_injecte_kwh
            
            cp1, cp2, cp3 = st.columns(3)
            cp1.metric(
                "Autoconsommation", 
                f"{best_taux_auto_config:,.1f} %".replace(",", " "), 
                delta=f"{gain_auto_conso:+.1f} %"
            )
            cp2.metric(
                "Autoproduction", 
                f"{best_taux_prod_config:,.1f} %".replace(",", " "), 
                delta=f"{gain_auto_prod:+.1f} %"
            )
            cp3.metric(
                "Surplus rejeté", 
                f"{best_surplus_config:,.0f} kWh".replace(",", " "), 
                delta=f"{diff_surplus:,.0f} kWh".replace(",", " "), 
                delta_color="inverse"
            )

            # --- RENTABILITÉ FINANCIÈRE ---
            st.write("#### 💰 Rentabilité du système idéal")
            roi = best_capex / best_gain_annuel if best_gain_annuel > 0 else 0
            economies_totale = best_economies
            
            cr1, cr2, cr3, cr4 = st.columns(4)
            cr1.metric("Investissement", f"{int(best_capex):,} {devise_val}".replace(",", " "))
            cr2.metric("Gain annuel net", f"{int(best_gain_annuel):,} {devise_val}/an".replace(",", " "), help="Calculé après déduction de la maintenance annuelle.")
            cr3.metric("Temps de retour (ROI)", f"{roi:,.1f} ans".replace(",", " "))
            cr4.metric(f"Économies (sur {duree_projet_val} ans)", f"{int(economies_totale):,} {devise_val}".replace(",", " "), help="Gain financier net total cumulé sur la durée de vie du projet, moins l'investissement initial.")

                        # --- DÉCOMPOSITION DES REVENUS ---
            with st.expander("📊 Détail des revenus annuels"):
                col_rev1, col_rev2 = st.columns(2)
                with col_rev1:
                    st.write(f"**Économies Autoconsommation :** {int(gain_autoconsommation):,} {devise_val}".replace(",", " "))
                    st.write(f"**Vente Surplus Solaire :** {int(gain_vente_surplus):,} {devise_val}".replace(",", " "))
                    if autoriser_arbitrage_val or autoriser_prix_dynamiques_val or (offre_france_val == "Tarif bleu particulier"):
                        st.write(f"**Gain Arbitrage / Différentiel HP-HC :** {int(gain_arbitrage):,} {devise_val}".replace(",", " "))
                with col_rev2:
                    if autoriser_ecretage_val:
                        st.write("**Écrêtage de pointe :**")
                        st.write(f"👉 Abonnement initial : {int(abonnement_val_val)} kVA")
                        p_pointe_init = max(courbe_conso_val_calc) if courbe_conso_val_calc else 0
                        st.write(f"👉 Puissance de pointe : {p_pointe_init:,.1f} kW".replace(",", " "))
                        # Calcul des frais de dépassement initiaux
                        frais_dep_init = 0
                        if st.session_state.get("pays_selectionne") == "France":
                            nb_h_dep_init = sum(1 for c_i in courbe_conso_val_calc if c_i > abonnement_val_val + 0.01)
                            frais_dep_init = nb_h_dep_init * taxe_puissance_annuelle_val
                        else: # Suisse
                            # On simule le pic mensuel
                            idx_h_s = 0
                            jours_m_s = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
                            for m_s in range(12):
                                hs_s = jours_m_s[m_s] * 24
                                p_init_s = max(courbe_conso_val_calc[idx_h_s:idx_h_s+hs_s])
                                frais_dep_init += p_init_s * taxe_puissance_annuelle_val
                                idx_h_s += hs_s
                        st.write(f"👉 Frais liés au dépassement de pointe : {int(frais_dep_init):,} {devise_val}".replace(",", " "))
                        
                        st.write(f"👉 Nouvelle pointe moyenne (PV+Stockage) : {p_max_net:,.1f} kW".replace(",", " "))
                        
                        # Economies sur les frais de dépassement
                        # gain_actuel_details contient l'économie sur les dépassements avec l'abo actuel
                        # gain_ecretage contient le gain total retenu (optimal en France, pic mensuel en Suisse)
                        if st.session_state.get("pays_selectionne") == "France":
                            st.write(f"👉 Économies sur les frais de dépassement : {int(gain_actuel_details):,} {devise_val}/an".replace(",", " "))
                            st.write(f"👉 **Nouvel abonnement optimal conseillé : {int(nouvel_abo_propose)} kVA**")
                            # Petit rappel du gain total écrêtage (dépassements + gain sur part fixe)
                            st.write(f"👉 Gain annuel total écrêtage : {int(revenu_ecretage):,} {devise_val}/an".replace(",", " "))
                            
                            st.write("---")
                            # Comparaison des coûts annuels de puissance
                            # Coût 1 : Abo actuel + Dépassements restants (après batterie)
                            nb_h_dep_restants_actuel = (frais_dep_init - gain_actuel_details) / taxe_puissance_annuelle_val if taxe_puissance_annuelle_val > 0 else 0
                            cout_annuel_actuel = (abonnement_val_val * cout_abonnement_kva_val) + (nb_h_dep_restants_actuel * taxe_puissance_annuelle_val)
                            
                            # Coût 2 : Abo optimal + Dépassements restants (après batterie)
                            # On retrouve le coût des dépassements pour l'abo optimal
                            # gain_ecretage_optimal = (g_fixe_mensuel - c_dep_mensuel)
                            # Donc c_dep_annuel = (abo_init - abo_opt)*prix - revenu_ecretage
                            cout_dep_opt_annuel = max(0, ((abonnement_val_val - nouvel_abo_propose) * cout_abonnement_kva_val) - revenu_ecretage)
                            cout_annuel_optimal = (nouvel_abo_propose * cout_abonnement_kva_val) + cout_dep_opt_annuel
                            
                            st.write(f"📉 **Coûts annuels de puissance (Abonnement + Dépassements) :**")
                            st.write(f"❌ Avec abonnement actuel et lissage de pics : {int(cout_annuel_actuel):,} {devise_val}/an".replace(",", " "))
                            st.write(f"✅ Avec nouvel abonnement et lissage de pics : {int(cout_annuel_optimal):,} {devise_val}/an".replace(",", " "))
                        else: # Suisse
                            st.write(f"👉 Économies annuelles sur les frais de pointe : {int(revenu_ecretage):,} {devise_val}/an".replace(",", " "))
                            
                            st.write("---")
                            # Coût 1 : Pic mensuel actuel (somme sur 12 mois) + Abo
                            # Pour la suisse, frais_dep_init est déjà la somme des pics * taxe
                            # Le gain revenu_ecretage est la somme des (pic_init - pic_net) * taxe
                            cout_annuel_actuel = (abonnement_val_val * cout_abonnement_kva_val) + (frais_dep_init - revenu_ecretage)
                            # Pas d'abonnement optimal calculé pour la suisse, on montre juste le gain
                            st.write(f"📉 **Coûts annuels de puissance :**")
                            st.write(f"❌ Sans batterie : {int((abonnement_val_val * cout_abonnement_kva_val) + frais_dep_init):,} {devise_val}/an".replace(",", " "))
                            st.write(f"✅ Avec batterie et lissage : {int(cout_annuel_actuel):,} {devise_val}/an".replace(",", " "))
                        
                        st.write(f"ℹ️ *Lissage progressif : 50% Batterie / 50% Réseau*")
                    if autoriser_services_val:
                        st.write(f"**Revenu Services Systèmes :** {int(revenu_services):,} {devise_val}".replace(",", " "))
                    st.write(f"**Frais de maintenance (OPEX) :** -{int((best_pv_total * opex_pv_unit_val) + (best_capa_batt * opex_batt_unit_val)):,} {devise_val}".replace(",", " "))
                
                st.write("---")
                st.write(f"**Total Gain Annuel Net :** {int(best_gain_annuel):,} {devise_val}".replace(",", " "))

            # --- NOUVEAU : GRAPHIQUE DE SYNTHÈSE DES SIMULATIONS ---
            st.write("---")
            if mode_ideal_val == "Favoriser l'autonomie du site":
                st.write(f"#### 📊 Analyse comparative : Autoproduction et Économies sur {duree_projet_val} ans")
            else:
                st.write(f"#### 📊 Analyse comparative : ROI et Économies sur {duree_projet_val} ans")
            
            if scenarios_comparaison:
                df_comp = pd.DataFrame(scenarios_comparaison)
                
                # Identification du système idéal pour le graphique
                label_ideal = f"{best_pv_total:,.1f} kWc / {int(best_capa_batt)} kWh".replace(",", " ")
                df_comp["IsIdeal"] = df_comp["Label"] == label_ideal
                
                fig_comp = go.Figure()
                
                # Économies (Barres bleues) - Axe Y1
                fig_comp.add_trace(go.Bar(
                    x=df_comp["Label"],
                    y=df_comp["Economies"],
                    name=f"Économies ({devise_val})",
                    marker_color="#AED6F1",
                    yaxis="y1",
                    hovertemplate="%{y:,.0f} " + devise_val
                ))

                # Ajout d'un marqueur spécial pour le système idéal
                df_ideal = df_comp[df_comp["IsIdeal"]]
                if not df_ideal.empty:
                    fig_comp.add_trace(go.Scatter(
                        x=df_ideal["Label"],
                        y=df_ideal["Economies"],
                        mode="markers",
                        name="SYSTÈME IDÉAL SELECTIONNÉ",
                        marker=dict(symbol="star", size=15, color="#F1C40F", line=dict(width=2, color="#B7950B")),
                        yaxis="y1",
                        hovertemplate="<b>SYSTÈME IDÉAL</b><br>Économies: %{y:,.0f} " + devise_val
                    ))
                
                # ROI (Courbe rouge) - Axe Y2 (%)
                fig_comp.add_trace(go.Scatter(
                    x=df_comp["Label"],
                    y=df_comp["ROI"],
                    name="ROI (ans)",
                    mode="lines+markers",
                    line=dict(color="#E74C3C", width=3),
                    marker=dict(size=8),
                    yaxis="y2",
                    hovertemplate="%{y:.1f} ans"
                ))
                
                # Autoproduction (Courbe jaune) - Axe Y2 (%)
                fig_comp.add_trace(go.Scatter(
                    x=df_comp["Label"],
                    y=df_comp["Autoproduction"],
                    name="Autoproduction (%)",
                    mode="lines+markers",
                    line=dict(color="#F7DC6F", width=3),
                    marker=dict(size=8),
                    yaxis="y2",
                    hovertemplate="%{y:.1f} %"
                ))

                # Cyclage annuel (Courbe violette) - Axe Y3
                fig_comp.add_trace(go.Scatter(
                    x=df_comp["Label"],
                    y=df_comp["Cyclage"],
                    name="Cyclage annuel",
                    mode="lines+markers",
                    line=dict(color="#A569BD", width=2, dash='dot'),
                    yaxis="y3",
                    hovertemplate="%{y} cycles"
                ))

                # Taux de remplissage moyen (Courbe verte) - Axe Y2 (%)
                fig_comp.add_trace(go.Scatter(
                    x=df_comp["Label"],
                    y=df_comp["Remplissage"],
                    name="Remplissage moyen (%)",
                    mode="lines+markers",
                    line=dict(color="#2ECC71", width=2, dash='dash'),
                    yaxis="y2",
                    hovertemplate="%{y:.1f} %"
                ))

                # Ratio Puissance appelée/installée (Batterie) (Courbe orange) - Axe Y2 (%)
                fig_comp.add_trace(go.Scatter(
                    x=df_comp["Label"],
                    y=df_comp["RatioPuissance"],
                    name="Ratio P. batterie (appelée/installée) (%)",
                    mode="lines+markers",
                    line=dict(color="#E67E22", width=2, dash='longdash'),
                    yaxis="y2",
                    hovertemplate="Ratio Batterie: %{y:.1f} %"
                ))

                # ZONES IDÉALES (Lignes horizontales et annotations)
                # ROI < 7.5 ans
                fig_comp.add_hline(y=7.5, line_dash="solid", line_color="#E74C3C", line_width=1, yref="y2")
                
                # Cyclage > 150
                fig_comp.add_hline(y=150, line_dash="solid", line_color="#A569BD", line_width=1, yref="y3")
                
                # Remplissage 40-60%
                fig_comp.add_hrect(y0=40, y1=60, fillcolor="#2ECC71", opacity=0.1, line_width=0, yref="y2")
                
                # Ratio Puissance batterie 60-80%
                fig_comp.add_hrect(y0=60, y1=80, fillcolor="#E67E22", opacity=0.1, line_width=0, yref="y2")

                fig_comp.update_layout(
                    height=600,
                    margin=dict(l=100, r=80, t=50, b=50),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="center", x=0.5),
                    hovermode="x unified",
                    xaxis=dict(domain=[0.15, 0.9]),
                    yaxis=dict(
                        title=f"Économies ({devise_val})",
                        title_font=dict(color="#AED6F1"),
                        tickfont=dict(color="#AED6F1"),
                        side="left",
                        position=0.15
                    ),
                    yaxis3=dict(
                        title="Cyclage annuel",
                        title_font=dict(color="#A569BD"),
                        tickfont=dict(color="#A569BD"),
                        side="left",
                        overlaying="y",
                        anchor="free",
                        position=0,
                        range=[0, max(df_comp["Cyclage"].max() * 1.2, 700) if not df_comp["Cyclage"].empty else 700],
                        showgrid=False
                    ),
                    yaxis2=dict(
                        title="Pourcentage (%) / ROI (ans)",
                        side="right",
                        overlaying="y",
                        range=[0, 105],
                        showgrid=True,
                        gridcolor="rgba(0,0,0,0.1)",
                        position=0.9
                    )
                )
                
                # Ajout d'annotations pour les zones idéales
                fig_comp.add_annotation(x=0.01, y=7.5, yref="y2", xref="paper", text="ROI idéal < 7.5 ans", showarrow=False, font=dict(color="#E74C3C", size=10), bgcolor="white", opacity=0.8)
                fig_comp.add_annotation(x=0.01, y=150, yref="y3", xref="paper", text="Cyclage idéal > 150", showarrow=False, font=dict(color="#A569BD", size=10), bgcolor="white", opacity=0.8)

                st.plotly_chart(fig_comp, use_container_width=True)

            # --- ANALYSE DE LA SOLLICITATION DE LA BATTERIE IDÉALE ---
            if simuler_batterie_val and best_capa_batt > 0:
                # On doit recalculer les flux pour le système idéal pour le graphique
                ratio_pv_ideal = best_pv_total / p_totale_max_toit if p_totale_max_toit > 0 else 0
                prod_h_ideal = [0.0] * 8760
                for item in profils_unitaires_par_pan:
                    p_pan_ideal = item['p_max'] * ratio_pv_ideal
                    for i in range(8760):
                        prod_h_ideal[i] += item['profil'][i] * p_pan_ideal
                
                stock_sol_i = 0.0
                stock_res_i = 0.0
                cap_utile_i = best_capa_batt * DOD
                p_batt_max_i = best_capa_batt * C_RATE
                liste_soc_i = []
                liste_charge_i = []
                liste_charge_res_i = []
                liste_decharge_i = []
                
                # Calcul du surplus solaire attendu pour chaque jour (pour l'anticipation)
                surplus_journalier_attendu_i = []
                for j in range(365):
                    s_jour = 0
                    for h in range(24):
                        idx = j*24 + h
                        s_jour += max(0, prod_h_ideal[idx] - courbe_conso_val_calc[idx])
                    surplus_journalier_attendu_i.append(s_jour)
                
                for h_idx, (ph, ch) in enumerate(zip(prod_h_ideal, courbe_conso_val_calc)):
                    c_charge = 0
                    c_charge_res = 0
                    c_decharge = 0
                    
                    prix_h = vecteur_prix_achat[h_idx]
                    h_jour = h_idx % 24
                    j_idx = h_idx // 24
                    est_prix_bas = False
                    if (autoriser_arbitrage_val or autoriser_prix_dynamiques_val):
                        prix_moyen = sum(vecteur_prix_achat) / 8760
                        seuil_charge = prix_moyen * 0.8
                        est_prix_bas = prix_h < seuil_charge
                        
                        # Anticipation solaire STRICTE
                        if est_prix_bas and 0 <= h_jour <= 6:
                            surplus_prevu = surplus_journalier_attendu_i[j_idx]
                            place_pour_reseau = max(0, cap_utile_i - surplus_prevu)
                            if (stock_sol_i + stock_res_i) >= place_pour_reseau:
                                est_prix_bas = False

                    # --- PRIORITÉ 1 : LE SOLAIRE ---
                    charge_sol_graph = 0
                    dech_sol_graph = 0
                    if ph >= ch:
                        dispo = ph - ch
                    
                        # Charger la batterie avec le surplus solaire
                        charge = min(dispo, (cap_utile_i - (stock_sol_i + stock_res_i)) / RENDEMENT_CHARGE, p_batt_max_i)
                        stock_sol_i += charge * RENDEMENT_CHARGE
                        c_charge = charge
                        charge_sol_graph = charge
                    else:
                        besoin = ch - ph
                    
                        # Décharger la batterie pour couvrir le besoin
                        total_s = stock_sol_i + stock_res_i
                        decharge_t = min(besoin / RENDEMENT_DECHARGE, total_s, p_batt_max_i)
                    
                        if total_s > 0:
                            r_sol = stock_sol_i / total_s
                            r_res = stock_res_i / total_s
                            stock_sol_i -= decharge_t * r_sol
                            stock_res_i -= decharge_t * r_res
                    
                        c_decharge = decharge_t * RENDEMENT_DECHARGE
                        dech_sol_graph = decharge_t

                    # --- PRIORITÉ 2 : L'ARBITRAGE RÉSEAU ---
                    if (autoriser_arbitrage_val or autoriser_prix_dynamiques_val) and est_prix_bas and (stock_sol_i + stock_res_i) < cap_utile_i:
                        p_dispo_batt = p_batt_max_i - (charge_sol_graph if ph >= ch else dech_sol_graph)
                        if p_dispo_batt > 0:
                            charge_res = min(p_dispo_batt, (cap_utile_i - (stock_sol_i + stock_res_i)) / RENDEMENT_CHARGE)
                            stock_res_i += charge_res * RENDEMENT_CHARGE
                            c_charge_res = charge_res

                    liste_soc_i.append(stock_sol_i + stock_res_i)
                    liste_charge_i.append(c_charge)
                    liste_charge_res_i.append(c_charge_res)
                    liste_decharge_i.append(c_decharge)

                st.write("---")
                st.subheader("Sollicitation de la batterie")
                
                total_charge_i = sum(liste_charge_i)
                total_charge_res_i = sum(liste_charge_res_i)
                total_decharge_i = sum(liste_decharge_i)
                cycles_complets_i = total_decharge_i / best_capa_batt if best_capa_batt > 0 else 0
                
                # Calcul des % de charge et décharge moyens journaliers (basés sur la capacité nominale)
                pct_charge_journalier = ((total_charge_i + total_charge_res_i) / 365) / best_capa_batt * 100 if best_capa_batt > 0 else 0
                pct_decharge_journalier = (total_decharge_i / 365) / best_capa_batt * 100 if best_capa_batt > 0 else 0
                
                # Taux de remplissage moyen (moyenne horaire du SOC / capacité nominale)
                if best_capa_batt > 0 and len(liste_soc_i) > 0:
                    remplissage_moyen_i = (sum(liste_soc_i) / len(liste_soc_i)) / best_capa_batt * 100
                else:
                    remplissage_moyen_i = 0

                c_sol1, c_sol2, c_sol3, c_sol4 = st.columns(4)
                c_sol1.metric("Cycles complets / an", f"{int(round(cycles_complets_i)):,}".replace(",", " "), help="Nombre de fois où la capacité totale de la batterie est déchargée en une année (Total décharge / Capacité nominale).")
                c_sol2.metric("Remplissage moyen", f"{remplissage_moyen_i:.1f} %", help="Moyenne de l'état de charge (SOC) de la batterie sur l'année.")
                
                if autoriser_arbitrage_val or autoriser_prix_dynamiques_val:
                    if autoriser_prix_dynamiques_val:
                        # Gain Arbitrage Dynamique
                        gain_annuel_brut_i = 0
                        s_temp_sim_sol = 0.0
                        s_temp_sim_res = 0.0
                        cap_utile_sim = best_capa_batt * DOD
                        p_batt_max_sim = best_capa_batt * C_RATE
                        
                        # Surplus journalier attendu
                        surplus_journalier_sim = []
                        for j in range(365):
                            s_j = 0
                            for h in range(24):
                                idx_j = j*24 + h
                                s_j += max(0, prod_h_ideal[idx_j] - courbe_conso_val_calc[idx_j])
                            surplus_journalier_sim.append(s_j)

                        for h_idx, (ph, ch) in enumerate(zip(prod_h_ideal, courbe_conso_val_calc)):
                            prix_h = vecteur_prix_achat[h_idx]
                            h_j = h_idx % 24
                            j_j = h_idx // 24
                            
                            # PRIORITÉ 1 : SOLAIRE
                            charge_s = 0
                            dech_s = 0
                            if ph >= ch:
                                gain_annuel_brut_i += ch * prix_h
                                dispo = ph - ch
                                charge_s = min(dispo, (cap_utile_sim - (s_temp_sim_sol + s_temp_sim_res)) / RENDEMENT_CHARGE, p_batt_max_sim)
                                s_temp_sim_sol += charge_s * RENDEMENT_CHARGE
                            else:
                                auto_h = ph
                                besoin = ch - ph
                                total_s_sim = s_temp_sim_sol + s_temp_sim_res
                                
                                # --- LOGIQUE DE LISSAGE (50% Batterie / 50% Réseau) ---
                                if autoriser_ecretage_val:
                                    # Nouveau : On couvre 50% du besoin peu importe le seuil
                                    besoin_a_couvrir = besoin * 0.5
                                    decharge_t = min(besoin_a_couvrir / RENDEMENT_DECHARGE, total_s_sim, p_batt_max_sim)
                                else:
                                    decharge_t = min(besoin / RENDEMENT_DECHARGE, total_s_sim, p_batt_max_sim)
                                
                                if total_s_sim > 0:
                                    r_sol = s_temp_sim_sol / total_s_sim
                                    r_res = s_temp_sim_res / total_s_sim
                                    s_temp_sim_sol -= decharge_t * r_sol
                                    s_temp_sim_res -= decharge_t * r_res
                                dech_s = decharge_t
                                auto_h += decharge_t * RENDEMENT_DECHARGE
                                gain_annuel_brut_i += auto_h * prix_h
                            
                            # PRIORITÉ 2 : ARBITRAGE
                            if (autoriser_arbitrage_val or autoriser_prix_dynamiques_val):
                                prix_moyen = sum(vecteur_prix_achat) / 8760
                                if prix_h < (prix_moyen * 0.8):
                                    est_prix_bas_sim = True
                                    if 0 <= h_j <= 6:
                                        place_r = max(0, cap_utile_sim - surplus_journalier_sim[j_j])
                                        if (s_temp_sim_sol + s_temp_sim_res) >= place_r:
                                            est_prix_bas_sim = False
                                    
                                    if est_prix_bas_sim:
                                        p_dispo = p_batt_max_sim - (charge_s if ph >= ch else dech_s)
                                        if p_dispo > 0:
                                            charge_r = min(p_dispo, (cap_utile_sim - (s_temp_sim_sol + s_temp_sim_res)) / RENDEMENT_CHARGE)
                                            s_temp_sim_res += charge_r * RENDEMENT_CHARGE
                                            gain_annuel_brut_i -= charge_r * prix_h

                        # Gain PV seul
                        gain_pv_seul = 0
                        for idx, (p, c) in enumerate(zip(prod_h_ideal, courbe_conso_val_calc)):
                            gain_pv_seul += min(p, c) * vecteur_prix_achat[idx]
                        gain_arb_i = gain_annuel_brut_i - gain_pv_seul
                    else:
                        gain_arb_i = total_charge_res_i * (tarif_hp - prix_hc_val)
                    
                    c_sol3.metric("Gain Arbitrage", f"{int(gain_arb_i):,} {devise_val}/an".replace(",", " "), help=f"Gain financier estimé grâce au pilotage intelligent de la batterie.")
                else:
                    c_sol3.metric("% Charge moy. jour", f"{pct_charge_journalier:.1f} %", help="Pourcentage moyen de la capacité nominale chargé chaque jour.")
                
                c_sol4.metric("% Décharge moy. jour", f"{pct_decharge_journalier:.1f} %", help="Pourcentage moyen de la capacité nominale déchargé chaque jour.")
                
                st.write("**Flux journaliers cumulés (kWh/jour)**")
                
                df_flux_i = pd.DataFrame({
                    "Charge Solaire": liste_charge_i,
                    "Charge Réseau (Arbitrage)": liste_charge_res_i,
                    "Decharge": [-d for d in liste_decharge_i]
                })
                df_daily_i = df_flux_i.groupby(df_flux_i.index // 24).sum()
                df_daily_i["Jour"] = df_daily_i.index + 1
                
                fig_sol_i = go.Figure()
                fig_sol_i.add_trace(go.Bar(
                    x=df_daily_i["Jour"],
                    y=df_daily_i["Charge Solaire"],
                    name="Charge (Solaire vers Batterie)",
                    marker_color="#2ECC71"
                ))
                if autoriser_arbitrage_val:
                    fig_sol_i.add_trace(go.Bar(
                        x=df_daily_i["Jour"],
                        y=df_daily_i["Charge Réseau (Arbitrage)"],
                        name="Charge (Réseau vers Batterie)",
                        marker_color="#3498DB"
                    ))
                fig_sol_i.add_trace(go.Bar(
                    x=df_daily_i["Jour"],
                    y=df_daily_i["Decharge"],
                    name="Décharge (Batterie vers Bâtiment)",
                    marker_color="#E74C3C"
                ))
                
                # S'assurer que les axes sont bien définis
                # fig_sol_i.update_xaxes(range=[0.5, 365.5], title="Jour de l'année")
                
                fig_sol_i.update_layout(
                    barmode='relative',
                    height=400,
                    margin=dict(l=0, r=0, t=0, b=0),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                    xaxis=dict(title="Jour de l'année", range=[0.5, 365.5]),
                    yaxis_title="kWh",
                    hovermode="x unified"
                )
                st.plotly_chart(fig_sol_i, use_container_width=True)
        else:
            st.error("Impossible de calculer le gisement solaire ou la consommation est nulle.")

        # --- GRAPHIQUE DE SUPERPOSITION ---
        st.write("---")
        st.header("📈 Comparaison Production vs Consommation")
        
        # On utilise les flux du système idéal/manuel s'ils existent
        dates = pd.date_range(start="2024-01-01", periods=8760, freq="H")
        
        # Par défaut, on affiche le PV de la section 2
        p_plot = prod_horaire_cumulee
        c_plot = courbe_conso_val_calc
        ch_plot = [0.0] * 8760
        de_plot = [0.0] * 8760
        
        # Recalculer les flux ici pour la configuration ACTUELLE affichée (best_pv_total, best_capa_batt)
        if 'best_pv_total' in locals() and best_pv_total > 0:
            ratio_pv_final = best_pv_total / p_totale_max_toit if p_totale_max_toit > 0 else 0
            p_plot = [0.0] * 8760
            for item in profils_unitaires_par_pan:
                p_pan_f = item['p_max'] * ratio_pv_final
                for i in range(8760):
                    p_plot[i] += item['profil'][i] * p_pan_f
            
            if best_capa_batt > 0:
                soc_f = 0.0
                # Pour le graphique de superposition, on doit aussi suivre l'origine du stock pour être cohérent
                stock_sol_f = 0.0
                stock_res_f = 0.0
                cap_utile_f = best_capa_batt * DOD
                p_batt_max_f = best_capa_batt * C_RATE
                ch_plot = []
                ch_res_plot = []
                de_plot = []
                
                # Calcul du surplus solaire attendu pour chaque jour
                surplus_journalier_attendu_f = []
                for j in range(365):
                    s_jour = 0
                    for h in range(24):
                        idx = j*24 + h
                        s_jour += max(0, p_plot[idx] - courbe_conso_val_calc[idx])
                    surplus_journalier_attendu_f.append(s_jour)
                
                for h_idx, (ph, ch) in enumerate(zip(p_plot, courbe_conso_val_calc)):
                    charge_f = 0
                    charge_res_f = 0
                    dech_f = 0
                    
                    prix_h = vecteur_prix_achat[h_idx]
                    h_jour = h_idx % 24
                    j_idx = h_idx // 24
                    est_prix_bas = False
                    if (autoriser_arbitrage_val or autoriser_prix_dynamiques_val):
                        prix_moyen = sum(vecteur_prix_achat) / 8760
                        seuil_charge = prix_moyen * 0.8
                        est_prix_bas = prix_h < seuil_charge
                        
                        # Anticipation solaire STRICTE
                        if est_prix_bas and 0 <= h_jour <= 6:
                            surplus_prevu = surplus_journalier_attendu_f[j_idx]
                            place_pour_reseau = max(0, cap_utile_f - surplus_prevu)
                            if (stock_sol_f + stock_res_f) >= place_pour_reseau:
                                est_prix_bas = False

                    # --- PRIORITÉ 1 : LE SOLAIRE ---
                    charge_sol_f = 0
                    dech_sol_f = 0
                    if ph >= ch:
                        # Consommation directe du solaire
                        dispo_solaire = ph - ch
                        
                        # Charger la batterie avec le surplus solaire
                        charge_f = min(dispo_solaire, (cap_utile_f - (stock_sol_f + stock_res_f)) / RENDEMENT_CHARGE, p_batt_max_f)
                        stock_sol_f += charge_f * RENDEMENT_CHARGE
                        charge_sol_f = charge_f
                    else:
                        besoin = ch - ph
                        
                        # Décharger la batterie pour couvrir le besoin
                        total_s = stock_sol_f + stock_res_f
                        
                        # --- LOGIQUE DE LISSAGE (PEAK SHAVING INTELLIGENT) ---
                        if autoriser_ecretage_val:
                            # --- LOGIQUE DE LISSAGE (50% Batterie / 50% Réseau) ---
                            # Nouveau : On couvre 50% du besoin peu importe le seuil
                            besoin_a_couvrir = besoin * 0.5
                            dech_t = min(besoin_a_couvrir / RENDEMENT_DECHARGE, total_s, p_batt_max_f)
                        else:
                            dech_t = min(besoin / RENDEMENT_DECHARGE, total_s, p_batt_max_f)
                        
                        if total_s > 0:
                            r_sol = stock_sol_f / total_s
                            r_res = stock_res_f / total_s
                            stock_sol_f -= dech_t * r_sol
                            stock_res_f -= dech_t * r_res
                        
                        dech_f = dech_t * RENDEMENT_DECHARGE
                        dech_sol_f = dech_t

                    # --- PRIORITÉ 2 : L'ARBITRAGE RÉSEAU ---
                    if (autoriser_arbitrage_val or autoriser_prix_dynamiques_val) and est_prix_bas and (stock_sol_f + stock_res_f) < cap_utile_f:
                        p_dispo_batt = p_batt_max_f - (charge_sol_f if ph >= ch else dech_sol_f)
                        if p_dispo_batt > 0:
                            charge_res_f = min(p_dispo_batt, (cap_utile_f - (stock_sol_f + stock_res_f)) / RENDEMENT_CHARGE)
                            stock_res_f += charge_res_f * RENDEMENT_CHARGE

                    ch_plot.append(charge_f)
                    ch_res_plot.append(charge_res_f)
                    de_plot.append(dech_f)
            else:
                ch_plot = [0.0] * 8760
                ch_res_plot = [0.0] * 8760
                de_plot = [0.0] * 8760
        else:
            p_plot = [0.0] * 8760
            ch_plot = [0.0] * 8760
            ch_res_plot = [0.0] * 8760
            de_plot = [0.0] * 8760

        df_total = pd.DataFrame({
            "Temps": dates,
            "Production PV (kW)": p_plot,
            "Consommation (kW)": courbe_conso_val_calc,
            "Charge Batterie Solaire (kW)": ch_plot,
            "Charge Batterie Réseau (kW)": ch_res_plot if 'ch_res_plot' in locals() and len(ch_res_plot) == 8760 else [0.0]*8760,
            "Décharge Batterie (kW)": de_plot
        })
        
        # Sélection du mois avec navigation fléchée DISCRÈTE ET À GAUCHE
        liste_mois = ["Janvier", "Février", "Mars", "Avril", "Mai", "Juin", "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre"]
        
        if "mois_idx" not in st.session_state:
            st.session_state.mois_idx = 5 # Juin par défaut
            
        # Mise en page compacte alignée à gauche
        c_nav_bloc, _ = st.columns([1.5, 3]) # On prend une petite portion à gauche
        with c_nav_bloc:
            c_prev, c_month, c_next = st.columns([1, 3, 1])
            with c_prev:
                if st.button("⬅️", key="prev_mois", help="Mois précédent"):
                    st.session_state.mois_idx = (st.session_state.mois_idx - 1) % 12
                    st.rerun()
            with c_month:
                mois_choix = st.selectbox(
                    "Mois",
                    options=liste_mois,
                    index=st.session_state.mois_idx,
                    label_visibility="collapsed",
                    key="select_mois"
                )
                st.session_state.mois_idx = liste_mois.index(mois_choix)
            with c_next:
                if st.button("➡️", key="next_mois", help="Mois suivant"):
                    st.session_state.mois_idx = (st.session_state.mois_idx + 1) % 12
                    st.rerun()
        
        # Filtrage des données par mois
        df_filtre = df_total[df_total['Temps'].dt.month == (st.session_state.mois_idx + 1)].copy()
        
        # Calcul de la courbe d'autoconsommation pour le hachurage
        df_filtre["Autoconsommation (kW)"] = df_filtre[["Production PV (kW)","Consommation (kW)"]].min(axis=1)

        # Calcul des flux nets (Achat/Vente sur le réseau)
        # On achète quand Consommation + Charge Batterie > Production + Décharge Batterie
        # On vend quand Production + Décharge Batterie > Consommation + Charge Batterie
        df_filtre["Flux Net (kW)"] = (df_filtre["Consommation (kW)"] + df_filtre["Charge Batterie Solaire (kW)"] + df_filtre["Charge Batterie Réseau (kW)"]) - (df_filtre["Production PV (kW)"] + df_filtre["Décharge Batterie (kW)"])
        df_filtre["Achat Réseau (kW)"] = df_filtre["Flux Net (kW)"].apply(lambda x: max(0, x))
        df_filtre["Vente Réseau (kW)"] = df_filtre["Flux Net (kW)"].apply(lambda x: max(0, -x))
        
        # Courbe de Soutirage Réseau (Grid Withdrawal) : ce que le bâtiment tire réellement au réseau
        df_filtre["Soutirage Réseau (kW)"] = df_filtre["Achat Réseau (kW)"]

        fig_superp = go.Figure()

        # Courbe de Consommation (Bleu pastel clair, lissée)
        fig_superp.add_trace(go.Scatter(
            x=df_filtre["Temps"],
            y=df_filtre["Consommation (kW)"],
            name="Consommation (kW)",
            line=dict(color='#AED6F1', width=2, shape='spline'),
            fill='none'
        ))

        # Courbe de Soutirage Réseau (Gris foncé, lissée)
        fig_superp.add_trace(go.Scatter(
            x=df_filtre["Temps"],
            y=df_filtre["Soutirage Réseau (kW)"],
            name="Soutirage Réseau (kW)",
            line=dict(color='#2C3E50', width=2, shape='spline'),
            fill='none'
        ))

        # Marqueurs Achat/Vente
        fig_superp.add_trace(go.Scatter(
            x=df_filtre[df_filtre["Achat Réseau (kW)"] > 0.1]["Temps"],
            y=df_filtre[df_filtre["Achat Réseau (kW)"] > 0.1]["Consommation (kW)"],
            mode='markers',
            name="Achat Réseau",
            marker=dict(symbol="triangle-up", color="#E74C3C", size=5),
            hovertemplate="Achat: %{text} kW",
            text=[f"{v:.1f}" for v in df_filtre[df_filtre["Achat Réseau (kW)"] > 0.1]["Achat Réseau (kW)"]]
        ))

        fig_superp.add_trace(go.Scatter(
            x=df_filtre[df_filtre["Vente Réseau (kW)"] > 0.1]["Temps"],
            y=df_filtre[df_filtre["Vente Réseau (kW)"] > 0.1]["Production PV (kW)"],
            mode='markers',
            name="Vente Réseau (Surplus)",
            marker=dict(symbol="triangle-down", color="#F1C40F", size=5),
            hovertemplate="Vente: %{text} kW",
            text=[f"{v:.1f}" for v in df_filtre[df_filtre["Vente Réseau (kW)"] > 0.1]["Vente Réseau (kW)"]]
        ))

        # Ajout de la ligne d'abonnement
        fig_superp.add_hline(
            y=abonnement_val_val,
            line_dash="dash",
            line_color="red",
            annotation_text="Limite d'abonnement",
            annotation_position="bottom right"
        )
        
        # Ajout de la ligne d'objectif d'écrêtage si active (France)
        if st.session_state.get("pays_selectionne") == "France" and autoriser_ecretage_val and 'nouvel_abo_propose' in locals():
            fig_superp.add_hline(
                y=nouvel_abo_propose,
                line_dash="dot",
                line_color="orange",
                annotation_text=f"Abonnement optimal ({int(nouvel_abo_propose)} kVA)",
                annotation_position="top right"
            )

        # Mise en évidence des dépassements de pointe
        seuil_alerte = nouvel_abo_propose if (st.session_state.get("pays_selectionne") == "France" and autoriser_ecretage_val and 'nouvel_abo_propose' in locals()) else abonnement_val_val
        df_depassement = df_filtre[df_filtre["Consommation (kW)"] > seuil_alerte].copy()
        if not df_depassement.empty:
            fig_superp.add_trace(go.Scatter(
                x=df_depassement["Temps"],
                y=df_depassement["Consommation (kW)"],
                mode='markers',
                name="Dépassement de pointe",
                marker=dict(color='red', size=6),
                hoverinfo='text',
                text=[f"Dépassement: {v:.1f} kW" for v in df_depassement["Consommation (kW)"]]
            ))

        # Zone d'Autoconsommation (Hachurée)
        fig_superp.add_trace(go.Scatter(
            x=df_filtre["Temps"],
            y=df_filtre["Autoconsommation (kW)"],
            name="Énergie autoconsommée (PV direct)",
            fill='tozeroy',
            mode='none',
            fillpattern=dict(shape="/", fgcolor="#F7DC6F", bgcolor="rgba(0,0,0,0)", fillmode="overlay"),
            hoverinfo='skip'
        ))

        # Courbe de Production PV (Jaune pastel)
        fig_superp.add_trace(go.Scatter(
            x=df_filtre["Temps"],
            y=df_filtre["Production PV (kW)"],
            name="Production PV (kW)",
            line=dict(color='#F7DC6F', width=2),
            fill='none'
        ))

        # AJOUT DES PRIX DYNAMIQUES SUR L'AXE Y2
        if autoriser_prix_dynamiques_val:
            fig_superp.add_trace(go.Scatter(
                x=df_filtre["Temps"],
                y=vecteur_prix_achat[df_filtre.index[0]:df_filtre.index[-1]+1],
                name=f"Prix Marché ({devise_val}/kWh)",
                line=dict(color='rgba(0,0,0,0.3)', width=1, dash='dot'),
                yaxis="y2",
                fill='none'
            ))

        # AJOUT DES FLUX BATTERIE
        if simuler_batterie_val and 'best_capa_batt' in locals() and best_capa_batt > 0:
            # Charge Batterie Solaire (VIOLET)
            fig_superp.add_trace(go.Scatter(
                x=df_filtre["Temps"],
                y=df_filtre["Charge Batterie Solaire (kW)"],
                name="Charge Solaire",
                line=dict(color='#A569BD', width=1.5, dash='dot'),
                fill='none'
            ))
            # Charge Batterie Réseau (BLEU)
            if autoriser_arbitrage_val or autoriser_prix_dynamiques_val:
                fig_superp.add_trace(go.Scatter(
                    x=df_filtre["Temps"],
                    y=df_filtre["Charge Batterie Réseau (kW)"],
                    name="Charge Réseau (Arbitrage)",
                    line=dict(color='#3498DB', width=1.5, dash='dot'),
                    fill='none'
                ))
            # Décharge Batterie (VERT)
            fig_superp.add_trace(go.Scatter(
                x=df_filtre["Temps"],
                y=df_filtre["Décharge Batterie (kW)"],
                name="Décharge Batterie",
                line=dict(color='#2ECC71', width=1.5, dash='dot'),
                fill='none'
            ))
        
        fig_superp.update_xaxes(
            dtick="D1", # Un repère par jour
            tickformat="%d %b", # Juste le jour et le mois
            gridcolor='lightgrey',
            gridwidth=1,
            griddash='dash' # Petits tirets pour la grille
        )
        
        fig_superp.update_yaxes(
            gridcolor='lightgrey',
            gridwidth=1,
            griddash='dash'
        )
        
        fig_superp.update_layout(
            height=450, 
            margin=dict(l=0, r=0, t=20, b=0), 
            xaxis_title=None,
            yaxis_title="kW",
            yaxis2=dict(
                title=f"{devise_val}/kWh",
                overlaying='y',
                side='right',
                showgrid=False,
                range=[0, max(vecteur_prix_achat)*1.2] if autoriser_prix_dynamiques_val else None
            ),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            hovermode="x unified",
            # Configuration du zoom/dézoom
            dragmode='zoom',
            modebar_add=['zoomIn2d', 'zoomOut2d', 'pan2d', 'resetScale2d']
        )
        
        # Pour le dézoom automatique vers le mois
        fig_superp.update_xaxes(range=[df_filtre["Temps"].min(), df_filtre["Temps"].max()], autorange=False)
        
        st.plotly_chart(fig_superp, use_container_width=True)
        
        if not lat or not lon:
            st.info("ℹ️ Note : Les coordonnées précises n'ont pas été trouvées. Simulation basée sur un productible standard de 1020 kWh/kWc (Suisse).")
        
    else:
        st.error("Impossible de calculer le gisement solaire. Vérifiez les paramètres de toiture.")
elif not adresse:
    st.info("En attente d'une adresse valide pour calculer le productible via PVGIS.")
else:
    st.header("Bilan énergétique de votre site")
    st.info("👋 Cliquez sur **Simuler** dans la barre latérale pour lancer l'étude et afficher les résultats.")
