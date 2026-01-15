import streamlit as st
import pandas as pd
import numpy as np
import requests
import math
import plotly.express as px
import plotly.graph_objects as go
from geopy.geocoders import Nominatim
from streamlit_searchbox import st_searchbox

# --- CONFIGURATION DE LA PAGE ---
st.set_page_config(page_title="Décarbonation Bâtiment", layout="wide")

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
        else:
            # API Photon pour la Suisse - Version simplifiée et plus robuste
            try:
                url_ph = "https://photon.komoot.io/api/"
                # On ne filtre pas par pays dans les paramètres car Photon est capricieux avec les filtres
                params_ph = {
                    "q": searchterm, 
                    "limit": 15, 
                    "lang": "fr"
                }
                res_ph = requests.get(url_ph, params=params_ph, timeout=3)
                if res_ph.status_code == 200:
                    for f in res_ph.json().get("features", []):
                        p = f.get("properties", {})
                        
                        # Filtrage manuel strict sur le code pays CH
                        if p.get("countrycode") == "CH":
                            parts = []
                            housenumber = p.get("housenumber")
                            street = p.get("street")
                            name = p.get("name")
                            postcode = p.get("postcode")
                            city = p.get("city")
                            
                            if housenumber: parts.append(housenumber)
                            
                            # Si 'street' est présent on l'utilise, sinon on prend 'name' si ce n'est pas la ville
                            if street:
                                parts.append(street)
                            elif name and name != city:
                                parts.append(name)
                                
                            if postcode: parts.append(postcode)
                            if city: parts.append(city)
                            parts.append("Suisse")
                            
                            full = " ".join(parts)
                            # On s'assure qu'on a au moins une rue et une ville
                            if len(parts) >= 3:
                                adresses.append(full)
            except Exception as e:
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
    pays_selectionne = st.sidebar.selectbox("Sélectionnez votre pays", ["France", "Suisse"], label_visibility="collapsed")
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
    type_toit = st.selectbox("Type", ["Plat", "Incliné"], label_visibility="collapsed")

if type_toit == "Plat":
    with col_mat:
        materiau = st.selectbox("Matériau", ["Bitumineux", "Gravier"], label_visibility="collapsed")
    
    # Choix de la variante pour toit plat
    variante_plat = st.sidebar.radio("Variante d'installation", ["Sud (Optimisé rendement)", "Est-Ouest (Optimisé surface)"], horizontal=True)
    
    inclinaison = 10
    if "Sud" in variante_plat:
        selection_orientations = ["Sud"]
    else:
        selection_orientations = ["Est", "Ouest"]
    
    # Pour toit plat, pas de méthode de mesure (toujours surface réelle projetée)
    surface_dispo = st.sidebar.number_input("Surface totale (m²)", min_value=1, value=300, step=1)
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
        materiau = st.selectbox("Matériau", ["Tuile", "Tôle", "Eternit"], label_visibility="collapsed")
    
    st.sidebar.subheader("Configuration des pans")
    mode_orientation = st.sidebar.radio("Type d'orientation", ["Mono-orientation", "Multi-orientations"], horizontal=True)
    
    orientations_possibles = ["Nord", "Nord-Est", "Est", "Sud-Est", "Sud", "Sud-Ouest", "Ouest", "Nord-Ouest"]
    
    donnees_pans = []
    
    if mode_orientation == "Mono-orientation":
        col1, col2 = st.sidebar.columns(2)
        with col1:
            orient = st.selectbox("Orientation", orientations_possibles, index=4)
        with col2:
            incli = st.number_input("Inclinaison (°)", min_value=0, max_value=90, value=10)
        
        # Méthode de mesure juste avant surface disponible
        mode_mesure = st.sidebar.radio(
            "Méthode de mesure des surfaces", 
            ["Vue aérienne", "Surface réelle"], 
            horizontal=True,
            help="**Vue aérienne** : La surface est calculée comme une projection horizontale. L'outil appliquera un correctif trigonométrique selon l'inclinaison pour obtenir la surface réelle du toit."
        )
        surf = st.sidebar.number_input("Surface totale (m²)", min_value=1, value=300)
        st.sidebar.markdown(f'<div style="font-size: 0.8rem; color: #666; margin-top: -15px; margin-bottom: 10px;">👉 {surf:,.0f} m²</div>'.replace(",", " "), unsafe_allow_html=True)
        donnees_pans.append({"orientation": orient, "inclinaison": incli, "surface": surf})
    else:
        # Méthode de mesure juste après type d'orientation
        mode_mesure = st.sidebar.radio(
            "Méthode de mesure des surfaces", 
            ["Vue aérienne", "Surface réelle"], 
            horizontal=True,
            help="**Vue aérienne** : La surface est calculée comme une projection horizontale. L'outil appliquera un correctif trigonométrique selon l'inclinaison pour obtenir la surface réelle du toit."
        )
        
        couples_possibles = {
            "Nord / Sud": ["Nord", "Sud"],
            "Est / Ouest": ["Est", "Ouest"],
            "Nord-Est / Sud-Ouest": ["Nord-Est", "Sud-Ouest"],
            "Nord-Ouest / Sud-Est": ["Nord-Ouest", "Sud-Est"]
        }
        choix_couple = st.sidebar.selectbox("Choisissez le couple d'orientations (2 pans)", list(couples_possibles.keys()))
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

        def update_from_individual():
            val_sum = sum(st.session_state[f"surf_{o}"] for o in selection_multi)
            st.session_state.surf_totale_multi = int(val_sum)

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
                    incli = st.number_input(f"Incl. {o}", min_value=0, max_value=90, value=10, key=f"incli_{o}", label_visibility="collapsed")
                with c3:
                    # On permet la modification individuelle de la surface par pan
                    surf_pan_individuelle = st.number_input(
                        f"Surf. {o}", 
                        min_value=0, 
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
with col_unit:
    unite_intro = st.selectbox(
        "Unité", 
        ["Ampères", "kVA"], 
        index=0,
        label_visibility="collapsed",
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
        help="La valeur des kVA est normalement notée dans votre contrat d'abonnement ou sur vos factures d'électricité."
    )

# --- ÉTAPE 5 : CONSOMMATION ÉNERGÉTIQUE ---
st.sidebar.write("📈 **Étape 4 : Consommation énergétique**")

profil_conso = st.sidebar.selectbox(
    "Type de bâtiment",
    ["Résidentiel", "Tertiaire / Bureaux", "Industriel"]
)

scénario_investissement = st.sidebar.radio(
    "Scénario d'investissement",
    ["Je suis propriétaire et consommateur sur site", "Je suis propriétaire et mon bâtiment est en location"],
    help="1. Propriétaire-consommateur : Vous investissez et réduisez votre propre facture. \n2. Propriétaire-bailleur : Vous investissez et revendez l'électricité à vos locataires."
)

# Modes disponibles : on enlève l'estimation pour l'industriel
modes_disponibles = ["Saisie manuelle (kWh)", "Télécharger une courbe de charge"]
if profil_conso != "Industriel":
    modes_disponibles.insert(0, "Estimation automatique")

mode_conso = st.sidebar.radio(
    "Données de consommation",
    modes_disponibles,
    horizontal=False
)

conso_annuelle_kwh = 0
df_courbe_charge = None

if mode_conso == "Estimation automatique":
    if profil_conso == "Résidentiel":
        col_nb, col_surf = st.sidebar.columns(2)
        with col_nb:
            nb_logements = st.number_input("Nb logements", min_value=1, value=1, step=1)
        with col_surf:
            surf_hab = st.number_input("Surface totale (m²)", min_value=1, value=100, step=10)
            st.markdown(f'<div style="font-size: 0.8rem; color: #666; margin-top: -15px; margin-bottom: 10px;">👉 {surf_hab:,.0f} m²</div>'.replace(",", " "), unsafe_allow_html=True)
        
        col_dpe, col_heat = st.sidebar.columns(2)
        with col_dpe:
            dpe = st.selectbox("DPE", ["A", "B", "C", "D", "E", "F", "G"], index=3)
        with col_heat:
            type_chauffe = st.selectbox("Chauffage", ["Électrique (PAC/Rad)", "Gaz", "Mazout"])
        
        ecs_elec = st.sidebar.checkbox("Eau Chaude Sanitaire Électrique", value=True)
        
        # Logique estimation Résidentiel (Ratios simplifiés)
        ratio_dpe = {"A": 50, "B": 80, "C": 120, "D": 190, "E": 250, "F": 330, "G": 450}[dpe]
        conso_base = 3000 * nb_logements # Base élec hors chauffage
        if "Électrique" in type_chauffe:
            conso_annuelle_kwh = (surf_hab * ratio_dpe) + conso_base
        else:
            conso_annuelle_kwh = conso_base + (1000 * nb_logements if ecs_elec else 0)

    elif profil_conso == "Tertiaire / Bureaux":
        surf_tert = st.sidebar.number_input("Surface totale (m²)", min_value=1, value=500, step=50)
        st.sidebar.markdown(f'<div style="font-size: 0.8rem; color: #666; margin-top: -15px; margin-bottom: 10px;">👉 {surf_tert:,.0f} m²</div>'.replace(",", " "), unsafe_allow_html=True)
        activite = st.sidebar.selectbox("Activité", ["Bureaux", "Commerce", "Restauration"])
        clim = st.sidebar.checkbox("Locaux climatisés", value=True)
        
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
        value=valeur_par_defaut,
        step=1000 if profil_conso == "Industriel" else 100,
        format="%d"
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
        prix_revente_locataire = st.number_input(f"Tarif vente au locataire ({devise}/kWh)", min_value=0.0, value=0.20, step=0.01)
    else:
        prix_achat = st.number_input(f"Prix Achat électricité ({devise}/kWh)", min_value=0.0, value=0.25, step=0.01)
        prix_revente_locataire = 0.0
with col_vente:
    prix_vente = st.number_input(f"Prix vente surplus électricité ({devise}/kWh)", min_value=0.0, value=0.05, step=0.01)

duree_projet = st.sidebar.number_input("Durée de vie du projet (ans)", min_value=1, max_value=50, value=25)

with st.sidebar.expander("💸 Coûts d'installation"):
    # Investissement initial
    capex_pv_unit = st.number_input(f"Investissement PV ({devise}/kWc)", min_value=0, value=int(capex_pv_estime), step=50, help=f"Valeur estimée pour une toiture {type_toit.lower()} {materiau.lower()}.")
    capex_batt_unit = st.number_input(f"Investissement Batterie ({devise}/kWh)", min_value=0, value=int(350 * coef_suisse), step=50)
    
    st.markdown("---")
    # Maintenance annuelle
    st.write("**Maintenance annuelle**")
    opex_pv_unit = st.number_input(f"Maintenance PV ({devise}/kWc/an)", min_value=0.0, value=6.0 * coef_suisse, step=1.0)
    opex_batt_unit = st.number_input(f"Maintenance Batterie ({devise}/kWh/an)", min_value=0.0, value=3.0 * coef_suisse, step=1.0)

# --- OBJECTIF DU SYSTÈME ---
st.sidebar.markdown("<h3 style='font-size: 1.2rem; font-weight: bold;'>Objectif du système</h3>", unsafe_allow_html=True)
mode_ideal = st.sidebar.radio(
    "Objectif du système",
    [
        "Favoriser le retour sur investissement", 
        "Favoriser l'autonomie du site"
    ],
    label_visibility="collapsed"
)

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

# Conversion kVA ou Amp en kW
# En résidentiel/tertiaire, 1 kVA ≈ 1 kW.
if unite_intro == "Ampères":
    puissance_intro_kw = (400 * intro_val * 1.732) / 1000
else:
    puissance_intro_kw = intro_val

# --- LOGIQUE DE CALCUL (PVGIS) ---
if adresse:
    lat, lon = obtenir_lat_lon(adresse)

    if lat and lon:
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

        for pan in donnees_pans:
            # ... (logique existante conservée pour nb_mods, puissance_pan, etc.)
            incli_pan = pan['inclinaison']
            surf_pan = pan['surface']
            orient_pan = pan['orientation']
            
            if mode_mesure == "Vue aérienne":
                surf_reelle = surf_pan / math.cos(math.radians(incli_pan))
            else:
                surf_reelle = surf_pan

            cote_theorique = math.sqrt(surf_reelle)
            surf_utile = (cote_theorique - 2 * pourtour_erp)**2 if cote_theorique > 1.8 else 0
            
            if type_toit == "Plat":
                dim_long = longueur_base + espacement_fixation
                dim_larg = largeur_base + espacement_fixation
                larg_projetee = dim_larg * math.cos(math.radians(10))
                ecartement_optimal = 0.15 if "Est-Ouest" in variante_plat else 0.45
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

        # Limitation par la puissance d'introduction
        puissance_retenue = min(puissance_pv_installable, puissance_intro_kw)
        
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
        # Préparation profil consommation
        if mode_conso == "Saisie manuelle (kWh)":
            courbe_conso = generer_profil_synthetique(profil_conso, conso_annuelle_kwh)
        elif mode_conso == "Estimation automatique":
            # On utilise la consommation calculée par les ratios
            courbe_conso = generer_profil_synthetique(profil_conso, conso_annuelle_kwh)
        else:
            # On récupère la courbe persistante depuis le session_state
            if "courbe_charge_file" in st.session_state and st.session_state.courbe_charge_file:
                courbe_conso = st.session_state.courbe_charge_file
                conso_annuelle_kwh = sum(courbe_conso)
            else:
                courbe_conso = [0.0] * 8760
                conso_annuelle_kwh = 0

        # Simulation heure par heure
        autoconsommation_kwh = 0
        surplus_injecte_kwh = 0
        for p, c in zip(prod_horaire_cumulee, courbe_conso):
            part_auto = min(p, c)
            autoconsommation_kwh += part_auto
            surplus_injecte_kwh += (p - part_auto)
        
        taux_autoconsommation = (autoconsommation_kwh / production_totale_an * 100) if production_totale_an > 0 else 0
        taux_autoproduction = (autoconsommation_kwh / conso_annuelle_kwh * 100) if conso_annuelle_kwh > 0 else 0
        
        productible_moyen = production_totale_an / puissance_retenue if puissance_retenue > 0 else 0
    else:
        productible_moyen = 0
        details_pvgis = []
        lat, lon = None, None
        puissance_pv_installable = 0
        puissance_retenue = 0
        production_totale_an = 0

    # --- AFFICHAGE DES RÉSULTATS ---
    st.header("Bilan énergétique de votre site")

    if not adresse:
        st.write("En attente de la saisie d'une adresse dans la barre latérale...")
    elif lat and lon:
        st.write("---")
        col1, col2 = st.columns(2)

        with col1:
            st.markdown("#### **📍 Bâtiment**")
            st.write(f"**Adresse :** {adresse}")
            
            # Affichage Introduction
            if unite_intro == "kVA":
                equiv_amp = (intro_val * 1000) / (400 * 1.732)
                st.write(f"**Introduction :** {intro_val:,.1f} kVA - {int(equiv_amp):,} A".replace(",", " "))
            else:
                equiv_kva = (400 * intro_val * 1.732) / 1000
                st.write(f"**Introduction :** {equiv_kva:,.1f} kVA - {int(intro_val):,} A".replace(",", " "))

            # Affichage Consommation
            label_conso = "Consommation locataires :" if "location" in scénario_investissement else "Consommation :"
            if conso_annuelle_kwh > 100000:
                st.write(f"**{label_conso}** {int(round(conso_annuelle_kwh/1000)):,} MWh/an".replace(",", " "))
            else:
                st.write(f"**{label_conso}** {conso_annuelle_kwh:,.0f} kWh/an".replace(",", " "))

            st.write(f"**Toiture :** {type_toit} ({materiau})")
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
            st.write(f"**Potentiel total de la toiture :** {puissance_pv_installable:,.1f} kWc".replace(",", " "))
        
        with col2:
            st.markdown("#### **☀️ Potentiel Solaire**")
            
            # 1 & 3. Puissance installable et modules sur la même ligne
            st.markdown(f'**Puissance installable :** {puissance_retenue:,.1f} kWc <span style="font-size: 0.9rem; color: #666; margin-left: 10px;">(soit {int(nb_modules_final):,} modules de 500 Wc)</span>'.replace(",", " "), unsafe_allow_html=True)
            
            # 2. La remarque en bleue
            st.markdown(f"""
                <div style="font-size: 0.75rem; color: #555; background-color: #e7f3fe; padding: 8px; border-radius: 5px; border-left: 5px solid #2196F3; margin-top: 5px; margin-bottom: 20px;">
                    💡 La puissance installable est le minimum entre la capacité de votre toit et la puissance de votre raccordement électrique.
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
            conso_mensuelle[m] = sum(courbe_conso[idx_h : idx_h + heures_mois])
            idx_h += heures_mois
            
        max_y_graph = max(max(conso_mensuelle) if conso_mensuelle else 0, max(prod_mensuelle_cumulee) if prod_mensuelle_cumulee else 0) * 1.1

        # Nouvelles colonnes pour les graphiques côte à côte
        col_g1, col_g2 = st.columns(2)
        
        with col_g1:
            # Graphique Conso Mensuelle
            label_graph_conso = "Consommation mensuelle locataires" if "location" in scénario_investissement else "Consommation mensuelle"
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
        st.write("Pour une installation photovoltaique seule conditionnée par la puissance de votre raccordement éléctrique actuel")
        
        # Calcul des KPI financiers pour la section 2
        capex_pv_s2 = puissance_retenue * capex_pv_unit
        opex_total_s2 = puissance_retenue * opex_pv_unit
        
        # Gain annuel = (Auto-consommé * Prix Achat/Locataire) + (Vendu * Prix Vente) - Maintenance
        if "location" in scénario_investissement:
            economies_elec_s2 = (autoconsommation_kwh * prix_revente_locataire)
        else:
            economies_elec_s2 = (autoconsommation_kwh * prix_achat)
            
        vente_surplus_s2 = (surplus_injecte_kwh * prix_vente)
        gain_annuel_s2 = economies_elec_s2 + vente_surplus_s2 - opex_total_s2
        
        roi_s2 = capex_pv_s2 / gain_annuel_s2 if gain_annuel_s2 > 0 else 0
        economies_totales_s2 = gain_annuel_s2 * duree_projet
        
        col_res1, col_res2, col_res3 = st.columns(3)
        col_res1.metric("Autoconsommation", f"{taux_autoconsommation:,.1f} %".replace(",", " "), help="Part de la production PV consommée sur place.")
        col_res2.metric("Autoproduction", f"{taux_autoproduction:,.1f} %".replace(",", " "), help="Part de la consommation totale couverte par le PV.")
        col_res3.metric("Surplus rejeté", f"{surplus_injecte_kwh:,.0f} kWh".replace(",", " "), help="Énergie réinjectée sur le réseau.")
        
        # KPI Financiers sur une ligne
        f_col1, f_col2, f_col3, f_col4 = st.columns(4)
        f_col1.metric("Investissement", f"{capex_pv_s2:,.0f} {devise}".replace(",", " "))
        f_col2.metric("Gain annuel", f"{gain_annuel_s2:,.0f} {devise}/an".replace(",", " "))
        f_col3.metric("ROI", f"{roi_s2:.1f} ans")
        f_col4.metric(f"Économies ({duree_projet} ans)", f"{economies_totales_s2:,.0f} {devise}".replace(",", " "))
        
        # --- SECTION SYSTÈME IDÉAL ---
        st.write("---")
        st.header("🏆 Votre système photovoltaïque et stockage idéal")
        
        with st.expander("🛠️ Tester manuellement une configuration"):
            col_m1, col_m2 = st.columns(2)
            p_man = col_m1.number_input("Puissance PV (kWc)", min_value=0.0, value=float(puissance_retenue), step=1.0)
            b_man = col_m2.number_input("Capacité Batterie (kWh)", min_value=0.0, value=0.0, step=1.0)
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
            for pan in donnees_pans:
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
                    if type_toit == "Plat":
                        dim_long = longueur_base + espacement_fixation
                        dim_larg = largeur_base + espacement_fixation
                        larg_projetee = dim_larg * math.cos(math.radians(10))
                        ecartement_opt = 0.15 if "Est-Ouest" in variante_plat else 0.45
                        surf_par_mod = dim_long * (larg_projetee + ecartement_opt)
                    else:
                        dim_long = longueur_base + espacement_fixation
                        dim_larg = largeur_base + espacement_fixation
                        surf_par_mod = dim_long * dim_larg
                    
                    if mode_mesure == "Vue aérienne":
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
            if profils_unitaires_par_pan and conso_annuelle_kwh > 0:
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
                    
                    for ph, ch in zip(prod_h_test, courbe_conso):
                        if ph >= ch:
                            auto_temp_kwh += ch
                            dispo = ph - ch
                            charge = min(dispo, (cap_utile_b - s_temp) / RENDEMENT_CHARGE, p_batt_max_test)
                            s_temp += charge * RENDEMENT_CHARGE
                        else:
                            auto_temp_kwh += ph
                            besoin = ch - ph
                            decharge = min(besoin / RENDEMENT_DECHARGE, s_temp, p_batt_max_test)
                            s_temp -= decharge
                            auto_temp_kwh += decharge * RENDEMENT_DECHARGE
                    
                    prod_annuelle_test = sum(prod_h_test)
                    t_prod = (auto_temp_kwh / conso_annuelle_kwh * 100) if conso_annuelle_kwh > 0 else 0
                    t_auto = (auto_temp_kwh / prod_annuelle_test * 100) if prod_annuelle_test > 0 else 0
                    surplus_test = max(0, prod_annuelle_test - auto_temp_kwh)
                    
                    tarif_valorisation_auto = prix_revente_locataire if "location" in scénario_investissement else prix_achat
                    gain_annuel_brut = (auto_temp_kwh * tarif_valorisation_auto) + (surplus_test * prix_vente)
                    opex_annuel = (p_test * opex_pv_unit) + (cap_b * opex_batt_unit)
                    gain_annuel_net = gain_annuel_brut - opex_annuel
                    capex_test = (p_test * capex_pv_unit) + (cap_b * capex_batt_unit)
                    van_test = (gain_annuel_net * duree_projet) - capex_test
                    
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
                        "Economies": round(van_test)
                    })
                else:
                    # MODE AUTO : Recherche de l'optimum
                    # On définit des paliers de test pour la puissance PV
                    p_totale_max_toit = sum(p['p_max'] for p in profils_unitaires_par_pan)
                    p_couverture_conso = conso_annuelle_kwh / productible_moyen if productible_moyen > 0 else 20.0
                    
                    # Nouvelle Logique PV selon les instructions
                    p_base = conso_annuelle_kwh / productible_moyen if productible_moyen > 0 else 20.0
                    
                    if "autonomie" in mode_ideal.lower():
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
                            
                            # Si p_test est petit (ex: 5 kWc), b_max peut être < 20. On s'assure d'un range cohérent.
                            if b_max < b_start:
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
                                
                                for ph, ch in zip(prod_h_test, courbe_conso):
                                    if ph >= ch:
                                        auto_temp_kwh += ch
                                        dispo = ph - ch
                                        charge = min(dispo, (cap_utile_b - s_temp) / RENDEMENT_CHARGE, p_batt_max_test)
                                        s_temp += charge * RENDEMENT_CHARGE
                                    else:
                                        auto_temp_kwh += ph
                                        besoin = ch - ph
                                        decharge = min(besoin / RENDEMENT_DECHARGE, s_temp, p_batt_max_test)
                                        s_temp -= decharge
                                        auto_temp_kwh += decharge * RENDEMENT_DECHARGE
                                
                                prod_annuelle_test = sum(prod_h_test)
                                t_prod = (auto_temp_kwh / conso_annuelle_kwh * 100) if conso_annuelle_kwh > 0 else 0
                                t_auto = (auto_temp_kwh / prod_annuelle_test * 100) if prod_annuelle_test > 0 else 0
                                surplus_test = max(0, prod_annuelle_test - auto_temp_kwh)
                                
                                tarif_valorisation_auto = prix_revente_locataire if "location" in scénario_investissement else prix_achat
                                gain_annuel_brut = (auto_temp_kwh * tarif_valorisation_auto) + (surplus_test * prix_vente)
                                opex_annuel = (p_test * opex_pv_unit) + (cap_b * opex_batt_unit)
                                gain_annuel_net = gain_annuel_brut - opex_annuel
                                capex_test = (p_test * capex_pv_unit) + (cap_b * capex_batt_unit)
                                
                                gain_cumule = gain_annuel_net * duree_projet
                                van_test = gain_cumule - capex_test
                                roi_test = capex_test / gain_annuel_net if gain_annuel_net > 0 else 99
                                
                                scenarios_comparaison.append({
                                    "Label": f"{p_test:,.1f} kWc / {int(cap_b)} kWh".replace(",", " "),
                                    "Autoproduction": t_prod,
                                    "Autoconsommation": t_auto,
                                    "ROI": round(roi_test, 1),
                                    "Economies": round(van_test)
                                })

                                if "autonomie" in mode_ideal.lower():
                                    # Règle de compromis pour l'autonomie : 
                                    # On cherche à maximiser l'autoproduction, mais on valorise aussi les économies.
                                    # Score = Autoproduction (%) + (Économies / 100 000) 
                                    # Cela permet de préférer un système qui gagne beaucoup plus d'argent même s'il produit un peu moins.
                                    score = t_prod + (van_test / 10000)
                                else:
                                    score = 1000 - roi_test if roi_test > 0 else -9999

                                if score > best_autoprod_score:
                                    best_autoprod_score = score
                                    best_pv_total = p_test
                                    best_capa_batt = cap_b
                                    best_gain_annuel = gain_annuel_net
                                    best_capex = capex_test
                                    best_taux_auto_config = t_auto
                                    best_taux_prod_config = t_prod
                                    best_surplus_config = surplus_test
                                    best_economies = van_test
                        
                        # Si on atteint 100% d'autoproduction, on continue de tester pour voir si une plus grosse batterie améliore encore la VAN (peu probable mais possible)
                        # On ne break plus systématiquement à 99% car on veut l'optimum financier
                
                # On ne break pas non plus prématurément sur le PV si l'objectif est financier
                # if best_taux_prod_config >= 99.0:
                #     break
            
            aug_intro_ideale = max(0.0, best_pv_total - puissance_intro_kw)
            
            # --- AFFICHAGE MÉTRIQUES IDÉALES ---
            c_id1, c_id2, c_id3 = st.columns(3)
            c_id1.metric("Puissance PV Idéale", f"{best_pv_total:,.1f} kWc".replace(",", " "), help="Puissance PV optimisant le compromis entre autonomie et rentabilité.")
            c_id2.metric("Stockage Idéal", f"{int(best_capa_batt):,} kWh".replace(",", " "), help="Capacité de stockage optimisant le compromis entre autonomie et rentabilité.")
            
            if aug_intro_ideale > 0:
                if unite_intro == "kVA":
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
            cr1.metric("Investissement", f"{int(best_capex):,} {devise}".replace(",", " "))
            cr2.metric("Gain annuel net", f"{int(best_gain_annuel):,} {devise}/an".replace(",", " "), help="Calculé après déduction de la maintenance annuelle.")
            cr3.metric("Temps de retour (ROI)", f"{roi:,.1f} ans".replace(",", " "))
            cr4.metric(f"Économies (sur {duree_projet} ans)", f"{int(economies_totale):,} {devise}".replace(",", " "), help="Gain financier net total cumulé sur la durée de vie du projet, moins l'investissement initial.")

            # --- NOUVEAU : GRAPHIQUE DE SYNTHÈSE DES SIMULATIONS ---
            st.write("---")
            if mode_ideal == "Favoriser l'autonomie du site":
                st.write(f"#### 📊 Analyse comparative : Autoproduction et Économies sur {duree_projet} ans")
            else:
                st.write(f"#### 📊 Analyse comparative : ROI et Économies sur {duree_projet} ans")
            
            if scenarios_comparaison:
                df_comp = pd.DataFrame(scenarios_comparaison)
                fig_comp = go.Figure()
                
                if mode_ideal == "Favoriser l'autonomie du site":
                    # Double ordonnée : Économies (Barres) / Autoproduction (Ligne)
                    fig_comp.add_trace(go.Bar(
                        x=df_comp["Label"],
                        y=df_comp["Economies"],
                        name=f"Économies sur {duree_projet} ans ({devise})",
                        marker_color="#AED6F1",
                        yaxis="y1"
                    ))
                    fig_comp.add_trace(go.Scatter(
                        x=df_comp["Label"],
                        y=df_comp["Autoproduction"],
                        name="Autoproduction (%)",
                        mode="lines+markers",
                        line=dict(color="#F7DC6F", width=3),
                        marker=dict(size=8),
                        yaxis="y2"
                    ))
                    fig_comp.update_layout(
                        yaxis=dict(title=f"Économies sur {duree_projet} ans ({devise})"),
                        yaxis2=dict(title="Autoproduction (%)", range=[0, 105], overlaying="y", side="right")
                    )
                else:
                    # Double ordonnée : Économies (Barres) / ROI (Ligne)
                    fig_comp.add_trace(go.Bar(
                        x=df_comp["Label"],
                        y=df_comp["Economies"],
                        name=f"Économies sur {duree_projet} ans ({devise})",
                        marker_color="#AED6F1",
                        yaxis="y1"
                    ))
                    fig_comp.add_trace(go.Scatter(
                        x=df_comp["Label"],
                        y=df_comp["ROI"],
                        name="ROI (ans)",
                        mode="lines+markers",
                        line=dict(color="#E74C3C", width=3),
                        yaxis="y2"
                    ))
                    fig_comp.update_layout(
                        yaxis=dict(title=f"Économies sur {duree_projet} ans ({devise})"),
                        yaxis2=dict(title="ROI (ans)", overlaying="y", side="right", range=[0, max(df_comp["ROI"]) * 1.2 if not df_comp["ROI"].empty else 20])
                    )
                
                fig_comp.update_layout(
                    height=500,
                    margin=dict(l=0, r=0, t=30, b=0),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                    hovermode="x unified"
                )
                st.plotly_chart(fig_comp, use_container_width=True)

            # --- ANALYSE DE LA SOLLICITATION DE LA BATTERIE IDÉALE ---
            if best_capa_batt > 0:
                # On doit recalculer les flux pour le système idéal pour le graphique
                ratio_pv_ideal = best_pv_total / p_totale_max_toit if p_totale_max_toit > 0 else 0
                prod_h_ideal = [0.0] * 8760
                for item in profils_unitaires_par_pan:
                    p_pan_ideal = item['p_max'] * ratio_pv_ideal
                    for i in range(8760):
                        prod_h_ideal[i] += item['profil'][i] * p_pan_ideal
                
                soc_i = 0.0
                cap_utile_i = best_capa_batt * DOD
                p_batt_max_i = best_capa_batt * C_RATE
                liste_soc_i = []
                liste_charge_i = []
                liste_decharge_i = []
                
                for ph, ch in zip(prod_h_ideal, courbe_conso):
                    c_charge = 0
                    c_decharge = 0
                    if ph >= ch:
                        dispo = ph - ch
                        charge = min(dispo, (cap_utile_i - soc_i) / RENDEMENT_CHARGE, p_batt_max_i)
                        soc_i += charge * RENDEMENT_CHARGE
                        c_charge = charge
                    else:
                        besoin = ch - ph
                        decharge = min(besoin / RENDEMENT_DECHARGE, soc_i, p_batt_max_i)
                        soc_i -= decharge
                        c_decharge = decharge * RENDEMENT_DECHARGE
                    liste_soc_i.append(soc_i)
                    liste_charge_i.append(c_charge)
                    liste_decharge_i.append(c_decharge)

                st.write("---")
                st.subheader("Sollicitation de la batterie")
                
                total_charge_i = sum(liste_charge_i)
                total_decharge_i = sum(liste_decharge_i)
                cycles_complets_i = total_decharge_i / best_capa_batt if best_capa_batt > 0 else 0
                
                # Calcul des % de charge et décharge moyens journaliers
                pct_charge_journalier = (total_charge_i / 365) / best_capa_batt * 100 if best_capa_batt > 0 else 0
                pct_decharge_journalier = (total_decharge_i / 365) / best_capa_batt * 100 if best_capa_batt > 0 else 0
                
                c_sol1, c_sol2, c_sol3 = st.columns(3)
                c_sol1.metric("Cycles complets / an", f"{int(round(cycles_complets_i)):,}".replace(",", " "), help="Nombre de fois où la capacité totale de la batterie est déchargée en une année.")
                c_sol2.metric("% Charge moyen journalier", f"{pct_charge_journalier:.1f} %", help="Pourcentage moyen de la capacité de la batterie chargé chaque jour.")
                c_sol3.metric("% Décharge moyen journalier", f"{pct_decharge_journalier:.1f} %", help="Pourcentage moyen de la capacité de la batterie déchargé chaque jour.")
                
                st.write("**Flux journaliers cumulés (kWh/jour)**")
                
                df_flux_i = pd.DataFrame({
                    "Charge": liste_charge_i,
                    "Decharge": [-d for d in liste_decharge_i]
                })
                df_daily_i = df_flux_i.groupby(df_flux_i.index // 24).sum()
                df_daily_i["Jour"] = df_daily_i.index + 1
                
                fig_sol_i = go.Figure()
                fig_sol_i.add_trace(go.Bar(
                    x=df_daily_i["Jour"],
                    y=df_daily_i["Charge"],
                    name="Charge (Solaire vers Batterie)",
                    marker_color="#2ECC71"
                ))
                fig_sol_i.add_trace(go.Bar(
                    x=df_daily_i["Jour"],
                    y=df_daily_i["Decharge"],
                    name="Décharge (Batterie vers Bâtiment)",
                    marker_color="#E74C3C"
                ))
                
                fig_sol_i.update_layout(
                    barmode='relative',
                    height=400,
                    margin=dict(l=0, r=0, t=0, b=0),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                    xaxis_title="Jour de l'année",
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
        c_plot = courbe_conso
        ch_plot = [0.0] * 8760
        de_plot = [0.0] * 8760
        
        # Si on est dans la section 3 (Système idéal/manuel), on écrase avec ses données
        if 'prod_h_ideal' in locals():
            p_plot = prod_h_ideal
            if 'liste_charge_i' in locals():
                ch_plot = liste_charge_i
                de_plot = liste_decharge_i

        # Pour plus de sécurité et de cohérence, on va recalculer les flux ici 
        # pour la configuration ACTUELLE affichée (best_pv_total, best_capa_batt)
        if 'best_pv_total' in locals() and best_pv_total > 0:
            ratio_pv_final = best_pv_total / p_totale_max_toit if p_totale_max_toit > 0 else 0
            p_plot = [0.0] * 8760
            for item in profils_unitaires_par_pan:
                p_pan_f = item['p_max'] * ratio_pv_final
                for i in range(8760):
                    p_plot[i] += item['profil'][i] * p_pan_f
            
            if best_capa_batt > 0:
                soc_f = 0.0
                cap_utile_f = best_capa_batt * DOD
                p_batt_max_f = best_capa_batt * C_RATE
                ch_plot = []
                de_plot = []
                for ph, ch in zip(p_plot, courbe_conso):
                    charge_f = 0
                    dech_f = 0
                    if ph >= ch:
                        dispo = ph - ch
                        charge_f = min(dispo, (cap_utile_f - soc_f) / RENDEMENT_CHARGE, p_batt_max_f)
                        soc_f += charge_f * RENDEMENT_CHARGE
                    else:
                        besoin = ch - ph
                        dech_f = min(besoin / RENDEMENT_DECHARGE, soc_f, p_batt_max_f)
                        soc_f -= dech_f
                        dech_f = dech_f * RENDEMENT_DECHARGE
                    ch_plot.append(charge_f)
                    de_plot.append(dech_f)

        df_total = pd.DataFrame({
            "Temps": dates,
            "Production PV (kW)": p_plot,
            "Consommation (kW)": courbe_conso,
            "Charge Batterie (kW)": ch_plot,
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
        df_filtre["Autoconsommation (kW)"] = df_filtre[["Production PV (kW)", "Consommation (kW)"]].min(axis=1)

        fig_superp = go.Figure()

        # Courbe de Consommation (Bleu pastel clair, lissée)
        fig_superp.add_trace(go.Scatter(
            x=df_filtre["Temps"],
            y=df_filtre["Consommation (kW)"],
            name="Consommation (kW)",
            line=dict(color='#AED6F1', width=2, shape='spline'),
            fill='none'
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

        # AJOUT DES FLUX BATTERIE
        if best_capa_batt > 0:
            # Charge Batterie (VIOLET)
            fig_superp.add_trace(go.Scatter(
                x=df_filtre["Temps"],
                y=df_filtre["Charge Batterie (kW)"],
                name="Charge Batterie",
                line=dict(color='#A569BD', width=1.5, dash='dot'),
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
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            hovermode="x unified",
            # Configuration du zoom/dézoom
            dragmode='zoom',
            modebar_add=['zoomIn2d', 'zoomOut2d', 'pan2d', 'resetScale2d']
        )
        
        # Pour le dézoom automatique vers le mois
        # On peut forcer le range au chargement mais laisser l'utilisateur zoomer
        fig_superp.update_xaxes(range=[df_filtre["Temps"].min(), df_filtre["Temps"].max()], autorange=False)
        
        st.plotly_chart(fig_superp, use_container_width=True)
        
    else:
        st.error("Impossible de calculer le gisement solaire. Vérifiez les paramètres de toiture.")
else:
    st.info("En attente d'une adresse valide pour calculer le productible via PVGIS.")
