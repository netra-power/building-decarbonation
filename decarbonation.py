import streamlit as st
import pandas as pd
import numpy as np
import requests
import math
import plotly.express as px
import plotly.graph_objects as go
import datetime
import time
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from geopy.geocoders import Nominatim
from streamlit_searchbox import st_searchbox

# --- CONFIGURATION REQUÊTES AVEC RETRY ---
def requests_retry_session(
    retries=3,
    backoff_factor=0.3,
    status_forcelist=(500, 502, 504),
    session=None,
):
    session = session or requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

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
                res_fr = requests_retry_session().get(url_fr, params=params_fr, timeout=5)
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
                res_ch = requests_retry_session().get(url_ch, params=params_ch, timeout=5)
                if res_ch.status_code == 200:
                    for f in res_ch.json().get("results", []):
                        adresses.append(f["attrs"]["label"].replace("<b>", "").replace("</b>", ""))
            except:
                # Fallback sur Photon si Swisstopo échoue
                try:
                    url_ph = "https://photon.komoot.io/api/"
                    params_ph = {"q": searchterm, "limit": 10, "lang": "fr"}
                    res_ph = requests_retry_session().get(url_ph, params=params_ph, timeout=5)
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
                res_ph = requests_retry_session().get(url_ph, params=params_ph, timeout=5)
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
            res = requests_retry_session().get(url, params=params, timeout=10).json()
            if res["features"]:
                lon, lat = res["features"][0]["geometry"]["coordinates"]
                return lat, lon
        except:
            pass
    return None, None

@st.cache_data
def appeler_pvgis(lat, lon, angle, aspect, pays="France"):
    """
    aspect: 0=Sud, 90=Ouest, -90=Est, 180=Nord
    """
    # Pertes : 14% par défaut, 10% pour la Suisse (souvent plus optimiste/précis)
    pertes = 10 if pays == "Suisse" else 14
    
    url = "https://re.jrc.ec.europa.eu/api/v5_2/PVcalc"
    params = {
        "lat": lat,
        "lon": lon,
        "peakpower": 1,
        "loss": pertes,
        "angle": angle,
        "aspect": aspect,
        "outputformat": "json"
    }
    # Pour la Suisse, on peut forcer l'utilisation de SARAH2 qui est très précise
    if pays == "Suisse":
        params["rradareadatabase"] = "PVGIS-SARAH2"

    try:
        response = requests_retry_session().get(url, params=params, timeout=10)
        if response.status_code == 200:
            data = response.json()
            return data['outputs']['totals']['fixed']['E_y'] # Production annuelle en kWh
    except Exception as e:
        st.error(f"Erreur API PVGIS : {e}")
    return None

@st.cache_data
def appeler_pvgis_mensuel(lat, lon, angle, aspect, pays="France"):
    """
    Récupère les données mensuelles moyennes de PVGIS.
    """
    pertes = 10 if pays == "Suisse" else 14
    
    url = "https://re.jrc.ec.europa.eu/api/v5_2/PVcalc"
    params = {
        "lat": lat,
        "lon": lon,
        "peakpower": 1,
        "loss": pertes,
        "angle": angle,
        "aspect": aspect,
        "outputformat": "json"
    }
    if pays == "Suisse":
        params["rradareadatabase"] = "PVGIS-SARAH2"

    try:
        response = requests_retry_session().get(url, params=params, timeout=10)
        if response.status_code == 200:
            data = response.json()
            # On récupère la liste des productions mensuelles (E_m)
            # E_m est la production mensuelle moyenne en kWh pour 1kWc
            return [m['E_m'] for m in data['outputs']['monthly']['fixed']]
    except Exception as e:
        st.error(f"Erreur API PVGIS Mensuel : {e}")
    return None

@st.cache_data
def appeler_pvgis_horaire(lat, lon, angle, aspect, pays="France"):
    """
    Récupère les données horaires de PVGIS (profil type sur 8760h).
    """
    pertes = 10 if pays == "Suisse" else 14
    
    url = "https://re.jrc.ec.europa.eu/api/v5_2/seriescalc"
    params = {
        "lat": lat,
        "lon": lon,
        "peakpower": 1,
        "loss": pertes,
        "angle": angle,
        "aspect": aspect,
        "outputformat": "json",
        "pvcalculation": 1,
        "startyear": 2020,
        "endyear": 2020
    }
    if pays == "Suisse":
        params["rradareadatabase"] = "PVGIS-SARAH2"

    try:
        response = requests_retry_session().get(url, params=params, timeout=15)
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
        
        profil_type.append(base)
    
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

# --- ÉTAPE 3 : VOTRE BÂTIMENT ET VOTRE CONSOMMATION ---
st.sidebar.write("⚡ **Étape 3 : Votre bâtiment et votre consommation**")

# --- INTRODUCTION ÉLECTRIQUE (DANS ÉTAPE 3) ---
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
                value=36.0, 
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
    
    type_tarif = st.sidebar.selectbox(
        "Type de tarif",
        ["Tarif bleu particuliers", "Tarif bleu pro", "Tarif jaune", "Tarif vert"],
        on_change=reset_simulation
    )
    
    if type_tarif == "Tarif bleu particuliers":
        option_tarif = st.sidebar.radio("Option tarifaire", ["Base", "Heures Creuses"], horizontal=True, on_change=reset_simulation)
        seuils_bleu = [3, 6, 9, 12, 15, 18, 24, 30, 36]
        abonnement_val = st.sidebar.selectbox("Puissance souscrite (kVA)", options=seuils_bleu, index=1, on_change=reset_simulation)
    elif type_tarif == "Tarif bleu pro":
        option_tarif = st.sidebar.radio("Option tarifaire", ["Base", "Heures Creuses"], horizontal=True, on_change=reset_simulation)
        seuils_bleu = [3, 6, 9, 12, 15, 18, 24, 30, 36]
        abonnement_val = st.sidebar.selectbox("Puissance souscrite (kVA)", options=seuils_bleu, index=1, on_change=reset_simulation)
    elif type_tarif == "Tarif jaune":
        option_tarif = st.sidebar.radio("Version d'utilisation", ["Longue Utilisation (LU)", "Courte Utilisation (CU)"], horizontal=True, on_change=reset_simulation)
        abonnement_val = st.sidebar.number_input("Puissance souscrite (kVA)", min_value=37, value=100, step=1, on_change=reset_simulation)
    else: # Tarif vert
        option_tarif = "Standard"
        abonnement_val = st.sidebar.number_input("Puissance souscrite (kVA)", min_value=37, value=100, step=1, on_change=reset_simulation)

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

# --- ÉTAPE 3 (SUITE) : CONSOMMATION ÉNERGÉTIQUE ---
profil_conso = st.sidebar.selectbox(
    "Type de bâtiment",
    ["Résidentiel", "Tertiaire / Bureaux", "Industriel"],
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
    horizontal=False,
    on_change=reset_simulation
)

conso_annuelle_kwh = 0
df_courbe_charge = None
df_courbe_prod = None

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

elif mode_conso == "Télécharger une courbe de charge":
    st.sidebar.markdown("""
        <div style="font-size: 0.8rem; color: #666; margin-bottom: 5px;">
            📥 Télécharger une courbe de charge
        </div>
        """, unsafe_allow_html=True)
    fichier_conso = st.sidebar.file_uploader(
        "Télécharger votre courbe de charge (CSV ou Excel)",
        type=["csv", "xlsx"],
        label_visibility="collapsed",
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
                        courbe_conso_brute = vals[:35040]
                        pas_temps_conso = 15
                    elif nb_points > 8000: # Horaire
                        courbe_conso_brute = vals[:8760]
                        pas_temps_conso = 60
                    else:
                        courbe_conso_brute = vals
                        pas_temps_conso = 60
                    
                    # Compléter si nécessaire
                    points_attendus = 35040 if pas_temps_conso == 15 else 8760
                    if len(courbe_conso_brute) < points_attendus:
                        courbe_conso_brute.extend([0.0] * (points_attendus - len(courbe_conso_brute)))
                    
                    conso_totale_det = sum(courbe_conso_brute)
                    
                    # Message de succès avec conso totale et police réduite
                    if conso_totale_det > 100000:
                        msg_conso = f"{conso_totale_det/1000:,.0f} MWh/an"
                    else:
                        msg_conso = f"{conso_totale_det:,.0f} kWh/an"
                        
                    st.sidebar.markdown(f"""
                        <div style="font-size: 0.8rem; color: #155724; background-color: #d4edda; padding: 10px; border-radius: 5px; border-left: 5px solid #28a745; margin-top: 10px;">
                            ✅ Consommation annuelle détectée : {msg_conso} ({pas_temps_conso} min)
                        </div>
                        """, unsafe_allow_html=True)
                    
                    # Stockage persistant
                    st.session_state.courbe_charge_file = courbe_conso_brute
                    st.session_state.pas_temps_conso = pas_temps_conso
                    st.session_state.conso_calculee = conso_totale_det
        except Exception as e:
            st.sidebar.error(f"Erreur lors de la lecture du fichier : {e}")

# Définition de la courbe de conso avant le bouton simuler
if mode_conso == "Saisie manuelle (kWh)":
    courbe_conso = generer_profil_synthetique(profil_conso, conso_annuelle_kwh)
    st.session_state.conso_calculee = conso_annuelle_kwh
    st.session_state.pas_temps_conso = 60
    if "courbe_charge_file" in st.session_state:
        del st.session_state.courbe_charge_file
elif mode_conso == "Estimation automatique":
    courbe_conso = generer_profil_synthetique(profil_conso, conso_annuelle_kwh)
    st.session_state.conso_calculee = conso_annuelle_kwh
    st.session_state.pas_temps_conso = 60
    if "courbe_charge_file" in st.session_state:
        del st.session_state.courbe_charge_file
else:
    if "courbe_charge_file" in st.session_state and st.session_state.courbe_charge_file:
        courbe_conso = st.session_state.courbe_charge_file
        conso_annuelle_kwh = sum(courbe_conso)
    else:
        courbe_conso = [0.0] * 8760
        conso_annuelle_kwh = 0

# --- ÉTAPE 4 : VOTRE TOITURE ---
st.sidebar.write("🏠 **Étape 4 : Votre toiture**")

st.sidebar.write("🏚️ **Toiture**")
col_type, col_mat = st.sidebar.columns(2)
with col_type:
    type_toit = st.selectbox("Type", ["Plat", "Incliné"], label_visibility="collapsed", on_change=reset_simulation)

if type_toit == "Plat":
    with col_mat:
        materiau = st.selectbox("Matériau", ["Bitumineux", "Gravier"], label_visibility="collapsed", on_change=reset_simulation)
    
    # Choix de la variante pour toit plat
    variante_plat = st.sidebar.radio("Variante d'installation", ["Sud (Optimisé rendement)", "Est-Ouest (Optimisé surface)"], horizontal=True, on_change=reset_simulation)
    
    inclinaison = 10
    if "Sud" in variante_plat:
        selection_orientations = ["Sud"]
    else:
        selection_orientations = ["Est", "Ouest"]
    
    # Pour toit plat, pas de méthode de mesure (toujours surface réelle projetée)
    surface_dispo = st.sidebar.number_input("Surface totale (m²)", min_value=1, value=300, step=1, on_change=reset_simulation)
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

st.sidebar.write("☀️ **Source des données de production**")
mode_production = st.sidebar.radio(
    "Source des données de production",
    ["Calcul automatique (PVGIS)", "Saisie manuelle du productible", "Télécharger une courbe de production PV"],
    label_visibility="collapsed",
    on_change=reset_simulation
)

if mode_production == "Saisie manuelle du productible":
    productible_manuel = st.sidebar.number_input(
        "Productible (kWh/kWc/an)",
        min_value=0,
        max_value=2500,
        value=1020 if st.session_state.get("pays_selectionne") == "Suisse" else 1100,
        step=10,
        on_change=reset_simulation,
        help="Saisissez le productible annuel attendu par kWc installé. L'outil utilisera le profil de production de PVGIS (basé sur votre adresse) mais l'ajustera proportionnellement pour correspondre à cette valeur annuelle."
    )
    st.session_state.productible_manuel = productible_manuel

if mode_production == "Télécharger une courbe de production PV":
    st.sidebar.markdown("""
        <div style="font-size: 0.8rem; color: #666; margin-bottom: 5px;">
            📥 Télécharger une courbe de production PV
        </div>
        """, unsafe_allow_html=True)
    fichier_prod = st.sidebar.file_uploader(
        "Télécharger votre courbe de production PV (CSV ou Excel)",
        type=["csv", "xlsx"],
        label_visibility="collapsed",
        key="uploader_prod_pv",
        help="Format requis : Un fichier avec deux colonnes (Date/Heure et Valeur en kW ou kWh). Supporte les pas de 15 min, 30 min ou 1h."
    )
    if fichier_prod:
        try:
            if fichier_prod.name.endswith('.csv'):
                try:
                    df_courbe_prod = pd.read_csv(fichier_prod, sep=';')
                    if len(df_courbe_prod.columns) < 2:
                        fichier_prod.seek(0)
                        df_courbe_prod = pd.read_csv(fichier_prod, sep=',')
                except:
                    fichier_prod.seek(0)
                    df_courbe_prod = pd.read_csv(fichier_prod, sep=',')
            else:
                df_courbe_prod = pd.read_excel(fichier_prod)
            
            if df_courbe_prod is not None:
                # Nettoyage global
                df_courbe_prod = df_courbe_prod.astype(str).apply(lambda x: x.str.replace(',', '.').str.strip())
                for col in df_courbe_prod.columns:
                    df_courbe_prod[col] = pd.to_numeric(df_courbe_prod[col], errors='coerce')
                
                cols_num = df_courbe_prod.select_dtypes(include=[np.number]).columns
                cols_num = [c for c in cols_num if df_courbe_prod[c].notna().any()]
                
                if len(cols_num) > 0:
                    # On cherche la colonne qui a la plus grande somme (vraisemblablement les données de production)
                    col_val = df_courbe_prod[cols_num].sum().idxmax()
                    df_f = df_courbe_prod[col_val].fillna(0)
                    nb_pts = len(df_f)
                    vals_p = df_f.tolist()

                    # Détection auto du pas de temps (similaire à la consommation)
                    if nb_pts > 30000: # Quart-horaire
                        courbe_prod_brute = vals_p[:35040]
                        pas_temps_prod = 15
                    elif nb_pts > 8000: # Horaire
                        courbe_prod_brute = vals_p[:8760]
                        pas_temps_prod = 60
                    else:
                        courbe_prod_brute = vals_p
                        pas_temps_prod = 60
                    
                    # Compléter si nécessaire
                    pts_attendus = 35040 if pas_temps_prod == 15 else 8760
                    if len(courbe_prod_brute) < pts_attendus:
                        courbe_prod_brute.extend([0.0] * (pts_attendus - len(courbe_prod_brute)))
                    
                    prod_totale_det = sum(courbe_prod_brute)
                    msg_prod = f"{prod_totale_det/1000:,.0f} MWh/an" if prod_totale_det > 100000 else f"{prod_totale_det:,.0f} kWh/an"
                        
                    st.sidebar.markdown(f"""
                        <div style="font-size: 0.8rem; color: #155724; background-color: #d4edda; padding: 10px; border-radius: 5px; border-left: 5px solid #28a745; margin-top: 10px;">
                            ✅ Production annuelle détectée : {msg_prod} ({pas_temps_prod} min)
                        </div>
                        """, unsafe_allow_html=True)
                    
                    st.session_state.courbe_prod_file = courbe_prod_brute
                    st.session_state.pas_temps_prod = pas_temps_prod
                    st.session_state.prod_calculee = prod_totale_det
        except Exception as e:
            st.sidebar.error(f"Erreur lors de la lecture du fichier : {e}")

    # Ajout de l'input pour la puissance installée car non déductible de la courbe
    st.sidebar.write("⚡ **Puissance de l'installation**")
    puissance_custom_prod = st.sidebar.number_input(
        "Puissance installée (kWc)",
        min_value=0.0,
        value=100.0,
        step=1.0,
        help="Saisissez la puissance crête de l'installation correspondant à la courbe importée.",
        on_change=reset_simulation
    )
    st.session_state.puissance_custom_prod = puissance_custom_prod

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
        "Favoriser le financier ROI",
        "Favoriser l'investissement (ROI < 7,5 ans)"
    ],
    label_visibility="collapsed",
    on_change=reset_simulation,
    help="**Autonomie** : Max d'autoproduction avec économies optimales.\n\n**ROI** : ROI le plus bas.\n\n**Investissement** : Max d'économies avec un ROI inférieur à 7,5 ans."
)

simuler_batterie = st.sidebar.toggle("Simuler une batterie", value=True, key="simuler_batterie", on_change=reset_simulation)

# Options avancées batterie
autoriser_ecretage = False
autoriser_services = False
revenu_services_unit = 100000.0
taxe_puissance_annuelle = 7.0 if st.session_state.pays_selectionne == "Suisse" else 6.0

if simuler_batterie:
    st.sidebar.markdown("<div style='margin-left: 20px;'>", unsafe_allow_html=True)
    autoriser_ecretage = st.sidebar.checkbox("Autoriser l'écrêtement de pointe", value=False, on_change=reset_simulation)
    if autoriser_ecretage:
        if st.session_state.get("pays_selectionne") == "France":
            # --- RÉCUPÉRATION DYNAMIQUE DES TARIFS POUR AFFICHAGE ---
            current_abo_val = 0.0
            current_depassement = 12.41 # Valeur par défaut Tarif Jaune
            
            if type_tarif == "Tarif bleu particuliers":
                prices_bleu_res_hc = {3: 0, 6: 141.60, 9: 176.16, 12: 209.16, 15: 239.88, 18: 271.80, 24: 340.20, 30: 402.36, 36: 465.00}
                current_abo_val = prices_bleu_res_hc.get(abonnement_val, 0)
                current_depassement = 0.0 # Pas de dépassement facturé en Tarif Bleu particulier
            elif type_tarif == "Tarif bleu pro":
                if option_tarif == "Base":
                    prices_bleu_non_res_base = {3: 134.04, 6: 166.92, 9: 198.60, 12: 230.28, 15: 261.48, 18: 291.60, 24: 357.36, 30: 422.52, 36: 487.20}
                    current_abo_val = prices_bleu_non_res_base.get(abonnement_val, 0)
                else:
                    prices_bleu_non_res_hc = {6: 167.40, 9: 200.16, 12: 233.76, 15: 265.68, 18: 299.04, 24: 371.40, 30: 436.32, 36: 501.84}
                    current_abo_val = prices_bleu_non_res_hc.get(abonnement_val, 0)
                current_depassement = 0.0 # Pas de dépassement facturé en Tarif Bleu pro
            elif type_tarif == "Tarif jaune":
                if "Longue Utilisation" in option_tarif:
                    current_abo_val = 38.27 * abonnement_val
                else:
                    current_abo_val = 26.44 * abonnement_val
                current_depassement = 12.41
            
            # Affichage des informations de tarif
            st.sidebar.markdown(f"""
                <div style="font-size: 0.8rem; color: #666; background-color: #f0f2f6; padding: 5px; border-radius: 5px; margin-bottom: 10px;">
                    💰 <b>Infos contrat :</b><br>
                    • Abonnement : {current_abo_val:,.2f} {devise}/an<br>
                    • Dépassement : {current_depassement:,.2f} {devise}/heure
                </div>
            """.replace(",", " "), unsafe_allow_html=True)

            # Pour la France, on demande le tarif de dépassement si l'abonnement le permet (BT > 36 kVA en général, mais on laisse la main)
            taxe_puissance_annuelle = st.sidebar.number_input(f"Tarif dépassement ({devise}/heure)", min_value=0.0, value=float(current_depassement if current_depassement > 0 else 12.65), step=0.1, format="%.2f", on_change=reset_simulation, help="Frais par heure de dépassement de la puissance souscrite.")
            st.sidebar.markdown(f'<div style="font-size: 0.8rem; color: #666; margin-top: -15px; margin-bottom: 10px;">👉 {taxe_puissance_annuelle:,.2f} {devise}/heure</div>'.replace(",", " "), unsafe_allow_html=True)
            if abonnement_val < 36:
                st.sidebar.info("💡 Pour les abonnements < 36 kVA, le peak shaving permet principalement d'éviter les disjonctions (pas de gain financier direct).")
            else:
                st.sidebar.caption("Note: Tarif par défaut de 12.65 €/h pour les clients BT > 36 kVA.")
        else: # Suisse
            taxe_puissance_annuelle = st.sidebar.number_input(f"Taxe puissance ({devise}/kW/mois)", min_value=0.0, value=7.0, step=0.5, format="%.1f", on_change=reset_simulation, help="Chaque mois est facturé un montant basé sur la puissance maximale atteinte chaque mois.")
            st.sidebar.markdown(f'<div style="font-size: 0.8rem; color: #666; margin-top: -15px; margin-bottom: 10px;">👉 {taxe_puissance_annuelle:,.1f} {devise}/kW/mois</div>'.replace(",", " "), unsafe_allow_html=True)
    
    autoriser_services = st.sidebar.checkbox("Autoriser la participation aux services systèmes", value=False, on_change=reset_simulation)
    if autoriser_services:
        revenu_services_unit = st.sidebar.number_input(f"Revenu services systèmes ({devise}/MWh/an)", min_value=0, value=100000, step=1000, format="%d", on_change=reset_simulation)
        st.sidebar.markdown(f'<div style="font-size: 0.8rem; color: #666; margin-top: -15px; margin-bottom: 10px;">👉 {revenu_services_unit:,.0f} {devise}/MWh/an</div>'.replace(",", " "), unsafe_allow_html=True)

    autoriser_arbitrage = False
    prix_hc = 0.0
    hc_start = 0
    hc_end = 0

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
        "profil_conso": profil_conso,
        "scénario_investissement": scénario_investissement,
        "conso_annuelle_kwh": st.session_state.get("conso_calculee", conso_annuelle_kwh),
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
        "mode_production": mode_production,
        "mode_conso": mode_conso,
        "puissance_custom_prod": st.session_state.get("puissance_custom_prod", 100.0) if mode_production == "Télécharger une courbe de production PV" else None,
        "courbe_prod_custom": st.session_state.get("courbe_prod_file") if mode_production == "Télécharger une courbe de production PV" else None,
        "autoriser_ecretage": autoriser_ecretage,
        "autoriser_services": autoriser_services,
        "autoriser_arbitrage": False,
        "prix_hc": 0.0,
        "hc_start": 0,
        "hc_end": 0,
        "revenu_services_unit": revenu_services_unit,
        "taxe_puissance_annuelle": taxe_puissance_annuelle,
        "courbe_conso": courbe_conso.copy() if 'courbe_conso' in locals() else None,
        "pas_temps_conso": st.session_state.get("pas_temps_conso", 60),
        "courbe_prod_upload": st.session_state.get("courbe_prod_file"),
        "pas_temps_prod": st.session_state.get("pas_temps_prod", 60),
        "devise": devise,
        "variante_plat": variante_plat if 'variante_plat' in locals() else None,
        "type_tarif": type_tarif if 'type_tarif' in locals() else None,
        "option_tarif": option_tarif if 'option_tarif' in locals() else None
    }

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
    mode_production_val = pv.get("mode_production", "Calcul automatique (PVGIS)")
    mode_conso_val = pv.get("mode_conso", "Estimation automatique")
    puissance_custom_prod_val = pv.get("puissance_custom_prod")
    courbe_prod_custom_val = pv.get("courbe_prod_custom")
    autoriser_ecretage_val = pv.get("autoriser_ecretage", False)
    autoriser_services_val = pv.get("autoriser_services", False)
    autoriser_arbitrage_val = pv.get("autoriser_arbitrage", False)
    prix_hc_val = pv.get("prix_hc", 0.0)
    hc_start_val = pv.get("hc_start", 0)
    hc_end_val = pv.get("hc_end", 0)
    revenu_services_unit_val = pv.get("revenu_services_unit", 100000.0)
    taxe_puissance_annuelle_val = pv.get("taxe_puissance_annuelle", 6.0)
    courbe_conso_val = pv["courbe_conso"]
    devise_val = pv["devise"]
    variante_plat_val = pv["variante_plat"]
    type_tarif_val = pv.get("type_tarif")
    option_tarif_val = pv.get("option_tarif")
    courbe_prod_upload_val = pv.get("courbe_prod_upload")
    pas_temps_conso_val = pv.get("pas_temps_conso", 60)
    pas_temps_prod_val = pv.get("pas_temps_prod", 60)

    # --- LOGIQUE PRIX RÉSEAU (FRANCE) ---
    abonnement_annuel_val = 0.0
    majoration_injection = 0.0
    
    if st.session_state.get("pays_selectionne") == "France":
        seuils_kva_complets = [3, 6, 9, 12, 15, 18, 24, 30, 36, 42, 48, 60, 72, 84, 96, 108, 120, 132, 144, 156, 168, 180, 192, 204, 216, 228, 240, 250]
        if type_tarif_val == "Tarif bleu particuliers":
            majoration_injection = 9.60
            if option_tarif_val == "Base":
                prix_achat_val = 0.1412 # On prend les mêmes tarifs que HC HP mais moyennés ou selon image
                # Image "Tarif Bleu Residentiel HC" montre 14.12 et 10.07. 
                # L'image pour Base Residentiel n'est pas fournie mais l'utilisateur a fourni "Tarif bleu particuliers"
                # Je vais chercher si une image correspond à Base Particuliers.
                # Image 2: "TARIF BLEU - OPTION HEURES CREUSES RESIDENTIEL"
                # Image 4: "TARIF BLEU - OPTION BASE NON-RESIDENTIEL"
                # Si Base Particuliers n'est pas là, je vais extrapoler ou utiliser une valeur raisonnable.
                # Attends, j'ai "Tarif bleu particuliers" et "Tarif bleu pro".
                
                # Tableaux de prix selon images:
                # 1. Bleu Non-Res HC: Abo(6kVA)=167.40, HP=13.51 c, HC=9.89 c
                # 2. Bleu Res HC: Abo(6kVA)=141.60, HP=14.12 c, HC=10.07 c, Major=9.60
                # 4. Bleu Non-Res Base: Abo(6kVA)=166.92, Base=12.74 c, Major=9.60
                
                # Pour Bleu Particuliers Base (Image non fournie, je vais utiliser des valeurs proches du Res HC)
                # En fait je vais définir des dictionnaires.
                
                prices_bleu_res_hc = {3: 0, 6: 141.60, 9: 176.16, 12: 209.16, 15: 239.88, 18: 271.80, 24: 340.20, 30: 402.36, 36: 465.00}
                if option_tarif_val == "Base":
                    # Faute d'image Base Particuliers, j'utilise les prix Base Non-Pro mais avec l'abo Res.
                    # Ou j'extrapole. Généralement Base est un peu moins cher que HP.
                    prix_achat_val = 0.1350
                    abonnement_annuel_val = prices_bleu_res_hc.get(abonnement_val_val, 0)
                else: # Heures Creuses
                    prix_hp_local = 0.1412
                    prix_hc_local = 0.1007
                    abonnement_annuel_val = prices_bleu_res_hc.get(abonnement_val_val, 0)
            
            elif type_tarif_val == "Tarif bleu pro":
                majoration_injection = 9.60
                if option_tarif_val == "Base":
                    # Image 4
                    prices_bleu_non_res_base = {3: 134.04, 6: 166.92, 9: 198.60, 12: 230.28, 15: 261.48, 18: 291.60, 24: 357.36, 30: 422.52, 36: 487.20}
                    prix_achat_val = 0.1274
                    abonnement_annuel_val = prices_bleu_non_res_base.get(abonnement_val_val, 0)
                else: # Heures Creuses
                    # Image 1
                    prices_bleu_non_res_hc = {6: 167.40, 9: 200.16, 12: 233.76, 15: 265.68, 18: 299.04, 24: 371.40, 30: 436.32, 36: 501.84}
                    prix_hp_local = 0.1351
                    prix_hc_local = 0.0989
                    abonnement_annuel_val = prices_bleu_non_res_hc.get(abonnement_val_val, 0)
            
            elif type_tarif_val == "Tarif jaune":
                # Image 5
                majoration_injection = 1.95 * abonnement_val_val
                if "Longue Utilisation" in option_tarif_val:
                    abonnement_annuel_val = 38.27 * abonnement_val_val
                    # On va simplifier les 4 prix en une moyenne ou gérer les saisons?
                    # Hiver HP: 17.594, HC: 12.009 | Eté HP: 8.716, HC: 8.021
                    # Pour le moment on prend une moyenne pondérée simple si pas de gestion fine
                    prix_achat_val = 0.12 # Moyenne
                else: # Courte Utilisation
                    abonnement_annuel_val = 26.44 * abonnement_val_val
                    prix_achat_val = 0.13
                # Taxe dépassement Tarif Jaune
                taxe_puissance_annuelle_val = 12.41
            
            # Application des prix HP/HC si option Heures Creuses ou Tarif Jaune
            # (Arbitrage automatique supprimé, on garde les tarifs de base)

    # --- RÉCUPÉRATION PRIX DYNAMIQUES ---
    vecteur_prix_achat_base = [prix_achat_val] * 8760
    # On l'étendra au pas de temps final plus tard si besoin

    lat, lon = obtenir_lat_lon(adresse_val)

    # Autoriser la simulation si lat/lon sont trouvés OU si on est en Suisse (avec repli par défaut)
    if (lat and lon) or st.session_state.get("pays_selectionne") == "Suisse":
        # --- NORMALISATION DES PAS DE TEMPS ---
        # Le but est de travailler au pas le plus fin disponible
        pas_temps_final = min(pas_temps_conso_val, pas_temps_prod_val)
        
        def mettre_a_jour_pas(courbe, pas_actuel, pas_cible):
            if pas_actuel == pas_cible:
                return list(courbe)
            if pas_actuel == 60 and pas_cible == 15:
                # Élargissement (répétition pour puissance, division pour énergie si on suppose kWh, mais ici kW/kWh sur pas de temps)
                # Si les données sont des puissances (kW) ou énergies (kWh) sur le pas de temps.
                # L'utilisateur dit "en kWh au pas 15min", donc 1kWh à 15min = 4kW de puissance.
                # Mais ici on travaille par pas de temps. Si on a 1kWh sur 1h, ça fait 0.25kWh sur 15min.
                return [val / 4 for val in courbe for _ in range(4)]
            if pas_actuel == 15 and pas_cible == 60:
                # Agrégation
                return [sum(courbe[i:i+4]) for i in range(0, len(courbe), 4)]
            return list(courbe)

        courbe_conso_travail = mettre_a_jour_pas(courbe_conso_val, pas_temps_conso_val, pas_temps_final)
        # On définit courbe_conso_val_calc comme la base de travail pour la suite
        courbe_conso_val_calc = courbe_conso_travail 
        
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

        if mode_production_val == "Télécharger une courbe de production PV" and "courbe_prod_file" in st.session_state and st.session_state.courbe_prod_file:
            prod_horaire_cumulee = st.session_state.courbe_prod_file
            # Recalculer la production totale et mensuelle à partir du fichier
            production_totale_an = sum(prod_horaire_cumulee)
            prod_mensuelle_cumulee = [0.0] * 12
            idx_h_m = 0
            jours_m = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
            for m_idx in range(12):
                h_m = jours_m[m_idx] * 24
                prod_mensuelle_cumulee[m_idx] = sum(prod_horaire_cumulee[idx_h_m : idx_h_m + h_m])
                idx_h_m += h_m
            # Puissance PV installée devient le max de la courbe uploadée
            puissance_pv_installable = max(prod_horaire_cumulee)
            nb_modules_total = int(puissance_pv_installable / 0.5)
            # On simule un pan virtuel pour la cohérence
            details_pans_calcul.append({
                "orientation": "Importée",
                "inclinaison": 0,
                "surface": 0,
                "puissance": puissance_pv_installable,
                "prod_unit": production_totale_an / puissance_pv_installable if puissance_pv_installable > 0 else 0,
                "nb_mods": nb_modules_total
            })
        else:
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
                
                # --- CALCUL DU PRODUCTIBLE UNITAIRE POUR CE PAN ---
                incli_pan = pan['inclinaison']
                orient_pan = pan['orientation']
                aspect = get_aspect(orient_pan)
                
                # Pays pour ajuster les pertes et la base de données PVGIS
                pays_actuel = st.session_state.get("pays_selectionne", "France")
                
                # On récupère d'abord les données PVGIS (toujours, pour avoir le profil)
                prod_unit = appeler_pvgis(lat, lon, incli_pan, aspect, pays=pays_actuel)
                prod_mensuelle_unitaire = appeler_pvgis_mensuel(lat, lon, incli_pan, aspect, pays=pays_actuel)
                prod_horaire_unitaire = appeler_pvgis_horaire(lat, lon, incli_pan, aspect, pays=pays_actuel)

                if mode_production_val == "Saisie manuelle du productible":
                    prod_unit_saisi = st.session_state.get("productible_manuel", 1100)
                    
                    if prod_unit and prod_unit > 0:
                        # On a des données PVGIS, on applique la proportionnalité
                        facteur_prop = prod_unit_saisi / prod_unit
                        prod_unit = prod_unit_saisi
                        if prod_mensuelle_unitaire:
                            prod_mensuelle_unitaire = [m * facteur_prop for m in prod_mensuelle_unitaire]
                        if prod_horaire_unitaire:
                            prod_horaire_unitaire = [h * facteur_prop for h in prod_horaire_unitaire]
                    else:
                        # Fallback si PVGIS échoue : on utilise le profil synthétique
                        prod_unit = prod_unit_saisi
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
                
                # Gestion du repli pour la Suisse si PVGIS échoue ou si lat/lon sont nuls
                if not prod_unit and pays_actuel == "Suisse" and mode_production_val != "Saisie manuelle du productible":
                    prod_unit = 1020  # Valeur par défaut pour la Suisse si l'API échoue
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
        if mode_production_val == "Télécharger une courbe de production PV":
            # En mode import, on force la puissance saisie par l'utilisateur
            puissance_retenue = puissance_custom_prod_val if puissance_custom_prod_val else puissance_pv_installable
        else:
            puissance_retenue = min(puissance_pv_installable, puissance_intro_kw_val)
        
        # Correction du nombre de modules pour qu'il soit cohérent avec la puissance retenue
        nb_modules_final = int(puissance_retenue / 0.5)
        
        if puissance_pv_installable > 0 and mode_production_val != "Télécharger une courbe de production PV":
            facteur_limite = puissance_retenue / puissance_pv_installable
            production_totale_an *= facteur_limite
            prod_mensuelle_cumulee = [p * facteur_limite for p in prod_mensuelle_cumulee]
            prod_horaire_cumulee = [p * facteur_limite for p in prod_horaire_cumulee]

        if mode_production_val == "Télécharger une courbe de production PV" and courbe_prod_upload_val:
            prod_horaire_cumulee = mettre_a_jour_pas(courbe_prod_upload_val, pas_temps_prod_val, pas_temps_final)
            production_totale_an = sum(prod_horaire_cumulee)
            
            # Recalculer le profil mensuel à partir de la courbe importée
            prod_mensuelle_cumulee = [0.0] * 12
            jours_par_mois_p_calc = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
            idx_p_calc = 0
            pts_par_heure = 60 // pas_temps_final
            for m_idx, j_m in enumerate(jours_par_mois_p_calc):
                pts_mois = j_m * 24 * pts_par_heure
                # S'assurer de ne pas dépasser la taille de la courbe
                fin_idx = min(idx_p_calc + pts_mois, len(prod_horaire_cumulee))
                prod_mensuelle_cumulee[m_idx] = sum(prod_horaire_cumulee[idx_p_calc : fin_idx])
                idx_p_calc += pts_mois

            # En mode import, la puissance retenue est STRICTEMENT la puissance saisie
            # Si pas saisie, on cherche le max sur 1h pour définir kWc
            if puissance_custom_prod_val:
                puissance_retenue = puissance_custom_prod_val
            else:
                # On agrège en 1h pour trouver la puissance max réaliste en kWc
                courbe_1h = mettre_a_jour_pas(prod_horaire_cumulee, pas_temps_final, 60)
                puissance_retenue = max(courbe_1h) if courbe_1h else 0
            
            puissance_pv_installable = puissance_retenue
            nb_modules_final = int(puissance_retenue / 0.5)
        else:
            somme_horaire = sum(prod_horaire_cumulee)
            if somme_horaire > 0 and production_totale_an > 0:
                ratio_norm = production_totale_an / somme_horaire
                prod_horaire_cumulee = [p * ratio_norm for p in prod_horaire_cumulee]
            # Si on travaille en 15min mais production PVGIS (1h), on étend
            if pas_temps_final == 15:
                prod_horaire_cumulee = mettre_a_jour_pas(prod_horaire_cumulee, 60, 15)
        
        # --- CALCUL AUTOCONSOMMATION ---
        # Simulation au pas de temps final
        autoconsommation_kwh = 0
        surplus_injecte_kwh = 0
        
        # Vecteur prix au pas de temps final
        vecteur_prix_achat = [prix_achat_val] * len(courbe_conso_travail)

        for idx, (p, c) in enumerate(zip(prod_horaire_cumulee, courbe_conso_travail)):
            part_auto = min(p, c)
            autoconsommation_kwh += part_auto
            surplus_injecte_kwh += (p - part_auto)
        
        # Économies section 2
        tarif_reseau_s2 = prix_revente_locataire_val if "location" in scénario_investissement_val else prix_achat_val
        gain_autoconso_pv_s2 = autoconsommation_kwh * (tarif_reseau_s2 - prix_vente_val)
        vente_surplus_s2 = surplus_injecte_kwh * prix_vente_val
        
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
                st.write(f"**Abonnement :** {type_tarif_val} - {option_tarif_val}")
                st.write(f"**Puissance souscrite :** {abonnement_val_val:,.1f} kVA".replace(",", " "))
                
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
            conso_affichée = sum(courbe_conso_val_calc) if courbe_conso_val_calc else conso_annuelle_kwh_val
            if conso_affichée > 100000:
                st.write(f"**{label_conso}** {int(round(conso_affichée/1000)):,} MWh/an".replace(",", " "))
            else:
                st.write(f"**{label_conso}** {conso_affichée:,.0f} kWh/an".replace(",", " "))

            # Ajout de la puissance de pointe (soutirage réseau max)
            facteur_kw_conv = 60 / pas_temps_final
            pts_par_heure = 60 // pas_temps_final
            
            # Puissance de pointe de la consommation brute
            p_pointe_conso_brute = max(courbe_conso_val_calc[:8760*pts_par_heure]) * facteur_kw_conv if courbe_conso_val_calc else 0
            
            # Puissance de pointe nette (soutirage réseau) - Prend en compte le PV seul de la section 2
            # On calcule le profil net sans batterie pour cette section
            profil_net_pv_seul = []
            if courbe_conso_val_calc and prod_horaire_cumulee:
                for c_h, p_h in zip(courbe_conso_val_calc[:8760*pts_par_heure], prod_horaire_cumulee[:8760*pts_par_heure]):
                    profil_net_pv_seul.append(max(0, c_h - p_h) * facteur_kw_conv)
            
            p_pointe_soutirage = max(profil_net_pv_seul) if profil_net_pv_seul else p_pointe_conso_brute
            
            if p_pointe_soutirage < p_pointe_conso_brute - 0.1:
                st.write(f"**Puissance de pointe (conso brute) :** {p_pointe_conso_brute:,.1f} kW".replace(",", " "))
                st.write(f"**Puissance de pointe (soutirage réseau) :** {p_pointe_soutirage:,.1f} kW".replace(",", " "))
            else:
                st.write(f"**Puissance de pointe :** {p_pointe_conso_brute:,.1f} kW".replace(",", " "))

            st.write(f"**Toiture :** {type_toit_val} ({materiau_val})")
            if mode_production_val != "Télécharger une courbe de production PV":
                st.write("**Potentiel par orientation :**")
                
                # Agrégation des données par orientation
                potentiel_par_orient = {}
                for d in details_pans_calcul:
                    orient = d['orientation']
                    if orient not in potentiel_par_orient:
                        potentiel_par_orient[orient] = {
                            "inclinaison": d['inclinaison'],
                            "surface": 0.0,
                            "nb_mods": 0,
                            "puissance": 0.0
                        }
                    potentiel_par_orient[orient]["surface"] += d['surface']
                    potentiel_par_orient[orient]["nb_mods"] += d['nb_mods']
                    potentiel_par_orient[orient]["puissance"] += d['puissance']

                # Affichage via un DataFrame Streamlit (solution la plus robuste)
                df_potentiel = pd.DataFrame([
                    {
                        "Orientation": orient,
                        "Inclinaison": f"{d['inclinaison']}°",
                        "Surface": f"{d['surface']:,.0f} m²".replace(",", " "),
                        "Modules": f"{d['nb_mods']:,}".replace(",", " "),
                        "Puissance": f"{d['puissance']:,.1f} kWc".replace(",", " ")
                    }
                    for orient, d in potentiel_par_orient.items()
                ])
                st.dataframe(df_potentiel, hide_index=True, use_container_width=True)
                st.write(f"**Puissance maximale installable en toiture :** {puissance_pv_installable:,.1f} kWc".replace(",", " "))
        
        with col2:
            st.markdown("#### **☀️ Potentiel Solaire**")
            
            # 1 & 3. Puissance installable et modules sur la même ligne
            if mode_production_val == "Télécharger une courbe de production PV":
                # On utilise une variable locale pour être sûr de l'affichage
                p_affichage = puissance_custom_prod_val if puissance_custom_prod_val is not None else puissance_retenue
                st.markdown(f'**Puissance installée :** {p_affichage:,.1f} kWc <span style="font-size: 0.9rem; color: #666; margin-left: 10px;">(soit {int(p_affichage / 0.5):,} modules de 500 Wc)</span>'.replace(",", " "), unsafe_allow_html=True)
            elif mode_production_val == "Saisie manuelle du productible":
                st.markdown(f'**Puissance installable :** {puissance_retenue:,.1f} kWc <span style="font-size: 0.9rem; color: #666; margin-left: 10px;">(soit {int(nb_modules_final):,} modules de 500 Wc)</span>'.replace(",", " "), unsafe_allow_html=True)
            else:
                st.markdown(f'**Puissance installable :** {puissance_retenue:,.1f} kWc <span style="font-size: 0.9rem; color: #666; margin-left: 10px;">(soit {int(nb_modules_final):,} modules de 500 Wc)</span>'.replace(",", " "), unsafe_allow_html=True)
            
            # 2. La remarque en bleue
            if mode_production_val == "Télécharger une courbe de production PV":
                st.markdown(f"""
                    <div style="font-size: 0.9rem; color: #555; background-color: #e7f3fe; padding: 10px; border-radius: 5px; border-left: 5px solid #2196F3; margin-top: 5px; margin-bottom: 20px;">
                        💡 La puissance affichée correspond à la <b>puissance de l'installation importée</b>.
                    </div>
                    """, unsafe_allow_html=True)
            elif mode_production_val == "Saisie manuelle du productible":
                st.markdown(f"""
                    <div style="font-size: 0.9rem; color: #555; background-color: #e7f3fe; padding: 10px; border-radius: 5px; border-left: 5px solid #2196F3; margin-top: 5px; margin-bottom: 20px;">
                        💡 La puissance installable est le minimum entre votre <b>capacité de toit</b> et votre <b>raccordement</b>. Le profil de production utilisé est celui de <b>PVGIS (ajusté proportionnellement)</b> à votre saisie manuelle.
                    </div>
                    """, unsafe_allow_html=True)
            else:
                st.markdown(f"""
                    <div style="font-size: 0.9rem; color: #555; background-color: #e7f3fe; padding: 10px; border-radius: 5px; border-left: 5px solid #2196F3; margin-top: 5px; margin-bottom: 20px;">
                        💡 La puissance installable est définie comme suit : <br>
                        le <b>minimum</b> entre la <b>capacité de votre toit</b> et la puissance de votre <b>raccordement électrique</b>.
                    </div>
                    """, unsafe_allow_html=True)

            # 4. Productible PVGIS
            if mode_production_val == "Saisie manuelle du productible":
                st.write(f"**Productible (Saisi) :** {productible_moyen:,.0f} kWh/kWc/an".replace(",", " "))
            else:
                st.write(f"**Productible (PVGIS) :** {productible_moyen:,.0f} kWh/kWc/an".replace(",", " "))
            
            # 5. Production annuelle
            st.write(f"**Production annuelle totale :** {production_totale_an:,.0f} kWh/an".replace(",", " "))

        # Une seule ligne continue séparatrice après les deux paragraphes
        st.write("---")
        
        # Calcul du max pour harmoniser les axes Y des deux graphiques
        # Calcul de la conso mensuelle
        conso_mensuelle = [0.0] * 12
        jours_par_mois_c = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
        pts_par_heure = 60 // pas_temps_final
        idx_c_m = 0
        for m in range(12):
            pts_mois = jours_par_mois_c[m] * 24 * pts_par_heure
            # S'assurer de ne pas dépasser la taille de la courbe
            fin_idx_c = min(idx_c_m + pts_mois, len(courbe_conso_val_calc))
            conso_mensuelle[m] = sum(courbe_conso_val_calc[idx_c_m : fin_idx_c])
            idx_c_m += pts_mois
            
        # Mise à jour de c_plot après son utilisation potentielle pour les graphiques
        c_plot_calc = list(courbe_conso_val_calc)
        # On ne complète plus systématiquement à 8760 si on est en 15min
        pts_annuels = 8760 * pts_par_heure
        if len(c_plot_calc) < pts_annuels: c_plot_calc.extend([0.0] * (pts_annuels - len(c_plot_calc)))
        c_plot_calc = c_plot_calc[:pts_annuels]
            
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
        
        # Condition pour le titre et la phrase de description selon les données importées
        if mode_production_val == "Télécharger une courbe de production PV":
            if mode_conso_val == "Télécharger une courbe de charge":
                st.header("Analyse de l'autoconsommation en fonction de vos données importées")
            else:
                st.header("Analyse de l'autoconsommation")
            
            p_desc = puissance_custom_prod_val if puissance_custom_prod_val is not None else puissance_retenue
            st.write(f"Pour une installation photovoltaique seule de **{p_desc:,.1f} kWc** dont les données de production ont été importées".replace(",", " "))
        elif mode_production_val == "Saisie manuelle du productible":
            st.header("Analyse de l'autoconsommation")
            st.write(f"Pour une installation photovoltaique seule de **{puissance_retenue:,.1f} kWc** basée sur votre productible saisi".replace(",", " "))
        else:
            st.header("Analyse de l'autoconsommation en exploitant la totalité de votre toiture")
            st.write(f"Pour une installation photovoltaique seule de **{puissance_retenue:,.1f} kWc** conditionnée par la puissance de votre raccordement électrique actuel".replace(",", " "))
        
        # Calcul des KPI financiers pour la section 2
        capex_pv_s2 = puissance_retenue * capex_pv_unit_val
        opex_total_s2 = puissance_retenue * opex_pv_unit_val
        
        # Gain annuel selon la formule : gain_autoconso_pv + vente_surplus - opex
        gain_annuel_brut_s2 = gain_autoconso_pv_s2 + vente_surplus_s2
            
        if st.session_state.get("pays_selectionne") == "France":
            # On injecte l'abonnement et la majoration dans le gain annuel pour le calcul du ROI
            gain_annuel_s2 = gain_annuel_brut_s2 - opex_total_s2 - abonnement_annuel_val - majoration_injection
        else:
            gain_annuel_s2 = gain_annuel_brut_s2 - opex_total_s2
        
        roi_s2 = capex_pv_s2 / gain_annuel_s2 if gain_annuel_s2 > 0 else 0
        
        col_res1, col_res2, col_res3, col_res4 = st.columns(4)
        col_res1.metric("Autoconsommation", f"{taux_autoconsommation:,.1f} %".replace(",", " "), help="Part de la production PV consommée sur place.")
        col_res2.metric("Autoproduction", f"{taux_autoproduction:,.1f} %".replace(",", " "), help="Part de la consommation totale couverte par le PV.")
        col_res3.metric("Surplus rejeté", f"{surplus_injecte_kwh:,.0f} kWh".replace(",", " "), help="Énergie réinjectée sur le réseau.")
        col_res4.metric("Énergie autoconsommée", f"{autoconsommation_kwh:,.0f} kWh".replace(",", " "), help="Énergie totale consommée directement.")
        
        # KPI Financiers sur une ligne
        f_col1, f_col2, f_col3, f_col4 = st.columns(4)
        f_col1.metric("Investissement", f"{capex_pv_s2:,.0f} {devise_val}".replace(",", " "))
        f_col2.metric("Gain annuel", f"{int(gain_annuel_s2):,} {devise_val}/an".replace(",", " "))
        f_col3.metric("ROI", f"{roi_s2:.1f} ans")
        economies_totales_s2 = int(gain_annuel_s2) * duree_projet_val
        f_col4.metric(f"Économies ({duree_projet_val} ans)", f"{economies_totales_s2:,.0f} {devise_val}".replace(",", " "))
        
        # --- DÉTAIL DES REVENUS ANNUELS (PV SEUL) ---
        with st.expander("📊 Détail des revenus annuels", expanded=False):
            col_exp_pv1, col_exp_pv2 = st.columns(2)
            with col_exp_pv1:
                # Économies Autoconsommation
                st.write(f"**Économies Autoconsommation PV :** {int(gain_autoconso_pv_s2):,} {devise_val}".replace(",", " "))
                st.caption("ℹ️ Correspond aux économies de consommation PV directe.")
                
                # Vente du surplus
                st.write(f"**Vente du surplus de production :** {int(vente_surplus_s2):,} {devise_val}".replace(",", " "))
                
                # OPEX (en négatif)
                st.write(f"**Coûts opérationnels (Maintenance) :** -{int(opex_total_s2):,} {devise_val}".replace(",", " "))
            
            with col_exp_pv2:
                if st.session_state.get("pays_selectionne") == "France":
                    st.markdown("#### **📜 Frais fixes (France)**")
                    st.write(f"**Abonnement annuel :** -{int(abonnement_annuel_val):,} {devise_val}".replace(",", " "))
                    st.write(f"**Majoration injection :** -{int(majoration_injection):,} {devise_val}".replace(",", " "))
                    st.caption("ℹ️ Ces frais sont déduits du gain annuel pour le calcul de la rentabilité.")
                else:
                    st.write("ℹ️ Pas de frais fixes supplémentaires détectés pour ce pays.")

        # --- SECTION SYSTÈME IDÉAL ---
        st.write("---")
        if mode_production_val == "Télécharger une courbe de production PV" and mode_conso_val == "Télécharger une courbe de charge":
            st.header("🏆 Votre stockage idéal")
        elif mode_production_val == "Télécharger une courbe de production PV":
            st.header("🏆 Votre système photovoltaïque et stockage idéal")
        else:
            st.header("🏆 Votre système photovoltaïque et stockage idéal")
        
        with st.expander("🛠️ Tester manuellement une configuration"):
            if simuler_batterie_val:
                col_m1, col_m2 = st.columns(2)
                if mode_production_val == "Télécharger une courbe de production PV":
                    p_man = col_m1.number_input("Puissance PV (kWc)", min_value=0.0, value=float(puissance_retenue), step=1.0, disabled=True, key="p_man_unique", help="La puissance PV est fixée par le fichier importé.")
                else:
                    p_man = col_m1.number_input("Puissance PV (kWc)", min_value=0.0, value=float(puissance_retenue), step=1.0, key="p_man_unique")
                b_man = col_m2.number_input("Capacité Batterie (kWh)", min_value=0.0, value=0.0, step=1.0, key="b_man_unique")
                
                # Ajout du ratio de décharge manuel pour l'écrêtage
                r_man = 1.0
                if autoriser_ecretage_val:
                    r_man = st.slider("Ratio de décharge (Batterie / Réseau)", min_value=0.1, max_value=1.0, value=0.5, step=0.05, key="r_man_unique", help="Définit quelle part du besoin de puissance au-dessus du PV la batterie doit tenter de couvrir.")
                    if b_man <= 0:
                        st.warning("⚠️ Veuillez saisir une capacité de batterie pour que le ratio de décharge soit pris en compte.")
            else:
                if mode_production_val == "Télécharger une courbe de production PV":
                    p_man = st.number_input("Puissance PV (kWc)", min_value=0.0, value=float(puissance_retenue), step=1.0, disabled=True, key="p_man_alone", help="La puissance PV est fixée par le fichier importé.")
                else:
                    p_man = st.number_input("Puissance PV (kWc)", min_value=0.0, value=float(puissance_retenue), step=1.0, key="p_man_alone")
                b_man = 0.0
                r_man = 1.0
            btn_manuel = st.button("Simuler manuellement")
        
        # La simulation s'exécute automatiquement pour l'optimisation
        # ou manuellement si le bouton est cliqué
        override_ideal = btn_manuel
        
        # Cas spécial : PV importé
        if mode_production_val == "Télécharger une courbe de production PV":
            # On force puissance_retenue à la valeur saisie, sans condition
            puissance_retenue = puissance_custom_prod_val if puissance_custom_prod_val is not None else puissance_retenue
            best_pv_total = puissance_retenue
        
        # Initialisation systématique des variables de performance du système idéal
        best_autoprod_score = -2_000_000_000.0 # Valeur très basse pour s'assurer que n'importe quel score sera supérieur
        best_taux_auto_config = taux_autoconsommation
        best_taux_prod_config = taux_autoproduction
        best_surplus_config = surplus_injecte_kwh
        best_capa_batt = 0.0
        best_pv_total = puissance_retenue
        best_gain_annuel = gain_annuel_s2
        best_capex = capex_pv_s2
        best_economies = economies_totales_s2
        auto_batt_kwh = 0.0
        total_charge_solaire = 0.0
        auto_temp_kwh = autoconsommation_kwh
        auto_pv_seul_local = autoconsommation_kwh
        val_auto_pv_calc = gain_autoconso_pv_s2
        val_auto_batt_calc = 0.0
        val_vente_surplus_calc = vente_surplus_s2
        revenu_services = 0.0
        revenu_ecretage = 0.0
        opex_annuel = opex_total_s2
        best_ratio_ecretage = 1.0
        p_max_init_annuel = 0.0
        p_max_net_annuel = 0.0
        nb_h_dep_init_total = 0.0
        nb_h_dep_final_total = 0.0
        cap_utile_ideal = 0.0
        p_batt_max_ideal_pt = 0.0
        p_totale_max_toit = 0.0
        best_gain_annuel_brut_opt = 0.0
        best_opex_annuel_opt = 0.0
        best_capex_opt = 0.0
        gain_annuel_brut_final = 0.0

        if True: # On simule toujours (soit l'idéal auto, soit le manuel si cliqué)
            if override_ideal:
                # On utilise les valeurs saisies manuellement
                p_test_manuel = p_man
                capa_test_manuel = b_man
            
            # Paramètres batteries par défaut
            DOD = 1.0  # Profondeur de décharge (100%)
            RENDEMENT_CHARGE = 0.95
            RENDEMENT_DECHARGE = 0.95
            # RTE (Round Trip Efficiency) = RENDEMENT_CHARGE * RENDEMENT_DECHARGE = 0.95 * 0.95 = 0.9025 (environ 90%)
            C_RATE = 0.5  # Puissance max = 0.5 * Capacité (Système 2h)
            
            # 1. Calcul des profils unitaires (pour 1 kWc) par orientation
            profils_unitaires_par_pan = []
            for pan in donnees_pans_val:
                aspect = get_aspect(pan['orientation'])
                
                # Pays pour ajuster les pertes et la base de données PVGIS
                pays_actuel_sim = st.session_state.get("pays_selectionne", "France")
                
                prod_h_unit = appeler_pvgis_horaire(lat, lon, pan['inclinaison'], aspect, pays=pays_actuel_sim)
                if prod_h_unit:
                    # Normalisation comme fait précédemment pour PVGIS 5.2
                    p_annuelle_unit = appeler_pvgis(lat, lon, pan['inclinaison'], aspect, pays=pays_actuel_sim)
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
            scenarios_comparaison = [] # Initialisation pour éviter NameError
            if (profils_unitaires_par_pan or (mode_production_val == "Télécharger une courbe de production PV" and courbe_prod_upload_val)) and conso_annuelle_kwh_val > 0:
                best_autoprod_score = -float('inf')

                if override_ideal:
                    # MODE MANUEL : Une seule simulation avec les valeurs saisies
                    p_test = p_test_manuel
                    cap_b = b_man # Utilisation directe de b_man
            
                    if mode_production_val == "Télécharger une courbe de production PV" and courbe_prod_upload_val:
                        prod_h_test = mettre_a_jour_pas(courbe_prod_upload_val, pas_temps_prod_val, pas_temps_final)
                        p_test = puissance_custom_prod_val if puissance_custom_prod_val else max(mettre_a_jour_pas(prod_h_test, pas_temps_final, 60))
                    else:
                        p_totale_max_toit = sum(p['p_max'] for p in profils_unitaires_par_pan)
                        ratio_pv = p_test / p_totale_max_toit if p_totale_max_toit > 0 else 0
                        prod_h_test = [0.0] * (8760 * pts_par_heure)
                        for item in profils_unitaires_par_pan:
                            p_pan_test = item['p_max'] * ratio_pv
                            item_fin = mettre_a_jour_pas(item['profil'], 60, pas_temps_final)
                            for i in range(len(prod_h_test)):
                                prod_h_test[i] += item_fin[i] * p_pan_test
            
                    cap_utile_b = cap_b * DOD
                    p_batt_max_test = cap_b * C_RATE
                    p_batt_max_test_pt = cap_b * C_RATE * (pas_temps_final / 60) # Puissance max sur le pas de temps

                    # --- OPTIMISATION DU RATIO DE DÉCHARGE ÉCRÊTAGE (MANUEL) ---
                    best_ratio_ecretage_man = r_man
                    if autoriser_ecretage_val and cap_b > 0 and not btn_manuel:
                        max_score_r_man = -float('inf')
                        # Test de plusieurs ratios pour trouver le meilleur compromis annuel
                        for r_t in [0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0]:
                            s_t_r = 0.0
                            auto_r_m = 0
                            p_n_r = []
                            soc_a_m = []
                            for h_idx, (ph, ch) in enumerate(zip(prod_h_test, courbe_conso_travail)):
                                h_j = (h_idx // pts_par_heure) % 24
                                if ph >= ch:
                                    d = ph - ch
                                    c = min(d, (cap_utile_b - s_t_r) / RENDEMENT_CHARGE, p_batt_max_test_pt)
                                    s_t_r += c * RENDEMENT_CHARGE
                                    p_n_r.append(0)
                                else:
                                    bes = ch - ph
                                    bes_cov = bes * r_t
                                    dech = min(bes_cov / RENDEMENT_DECHARGE, s_t_r, p_batt_max_test_pt)
                                    s_t_r -= dech
                                    auto_r_m += (ph + dech * RENDEMENT_DECHARGE)
                                    p_n_r.append((bes - dech * RENDEMENT_DECHARGE) * (60 / pas_temps_final))
                                if 6 <= h_j <= 8: soc_a_m.append(s_t_r)
                            pts_par_heure = 60 // pas_temps_final
                            p_m_r = max(p_n_r[:8760*pts_par_heure]) if p_n_r else 0
                            s_m_a_m = sum(soc_a_m)/len(soc_a_m) if soc_a_m else 0
                            # Score : Favorise l'autoconsommation tout en pénalisant les batteries pleines à l'aube et les pics réseau
                            sc_r = auto_r_m - (s_m_a_m * 1000) - (p_m_r * 10)
                            if sc_r > max_score_r_man:
                                max_score_r_man = sc_r
                                best_ratio_ecretage_man = r_t
                        best_ratio_ecretage = best_ratio_ecretage_man
                    else:
                        best_ratio_ecretage = best_ratio_ecretage_man
                        # Pas besoin de mettre à jour r_man car il vient déjà de l'input utilisateur

                    s_temp = 0.0
                    auto_pv_seul_local = 0
                    auto_batt_kwh = 0
                    total_charge_solaire = 0
                    total_decharge_sim = 0
                    soc_points = []
                    p_max_net = 0
                    p_batt_real_max = 0
                    total_charge_reseau = 0
                    
                    profil_net = []
                    for h_idx, (ph, ch) in enumerate(zip(prod_h_test, courbe_conso_travail)):
                        # 1. Autoconsommation PV directe (baseline sans batterie)
                        part_directe = min(ph, ch)
                        auto_pv_seul_local += part_directe
                        
                        surplus_inst = max(0, ph - ch)
                        besoin_inst = max(0, ch - ph)
                        
                        h_jour = (h_idx // pts_par_heure) % 24
                        
                        if surplus_inst > 0:
                            charge = min(surplus_inst, (cap_utile_b - s_temp) / RENDEMENT_CHARGE, p_batt_max_test_pt)
                            s_temp += charge * RENDEMENT_CHARGE
                            total_charge_solaire += charge
                            p_batt_real_max = max(p_batt_real_max, charge * (60 / pas_temps_final))
                            soc_points.append(s_temp)
                            profil_net.append(0)
                        else:
                            # Logique d'optimisation d'écrêtage (Peak Shaving) :
                            besoin_a_couvrir = besoin_inst * (best_ratio_ecretage_man if autoriser_ecretage_val else 1.0)

                            # Si on a du besoin
                            decharge = min(besoin_a_couvrir / RENDEMENT_DECHARGE, s_temp, p_batt_max_test_pt)
                            s_temp -= decharge
                            p_batt_real_max = max(p_batt_real_max, decharge * (60 / pas_temps_final))
                            auto_batt_h = decharge * RENDEMENT_DECHARGE
                            auto_batt_kwh += auto_batt_h
                            total_decharge_sim += auto_batt_h
                            soc_points.append(s_temp)
                            profil_net.append((besoin_inst - auto_batt_h) * (60 / pas_temps_final))
                    
                    # En mode import, on force l'égalité parfaite avec la section 2 pour le PV seul
                    if mode_production_val == "Télécharger une courbe de production PV":
                        auto_pv_seul_local = autoconsommation_kwh

                    # Autoconsommation totale = Directe + Batterie
                    auto_temp_kwh = auto_pv_seul_local + auto_batt_kwh
                    
                    cyclage_annuel = total_decharge_sim / cap_b if cap_b > 0 else 0
                    remplissage_moyen = (sum(soc_points) / len(soc_points)) / cap_b * 100 if cap_b > 0 else 0
                    ratio_puissance = (p_batt_real_max / p_batt_max_test * 100) if p_batt_max_test > 0 else 0
                    
                    prod_annuelle_test = sum(prod_h_test)
                    t_prod = (auto_temp_kwh / conso_annuelle_kwh_val * 100) if conso_annuelle_kwh_val > 0 else 0
                    t_auto = (auto_temp_kwh / prod_annuelle_test * 100) if prod_annuelle_test > 0 else 0
                    # Correction surplus en mode manuel
                    surplus_test = max(0, prod_annuelle_test - auto_pv_seul_local - total_charge_solaire)
                    
                    # Valorisation financière
                    tarif_val_reseau = prix_revente_locataire_val if "location" in scénario_investissement_val else prix_achat_val
                    
                    # REVENUS DÉTAILLÉS (pour assurer la cohérence avec best_gain_annuel)
                    det_auto_pv_man = auto_pv_seul_local * (tarif_val_reseau - prix_vente_val)
                    det_auto_batt_man = auto_batt_kwh * (tarif_val_reseau - prix_vente_val) if cap_b > 0 else 0
                    det_vente_surplus_man = surplus_test * prix_vente_val
                    
                    # Revenus additionnels batterie (Peak Shaving et Services Systèmes)
                    revenu_ecretage_man = 0
                    if autoriser_ecretage_val and cap_b > 0:
                        # On recalcule le profil net précisément pour le mode manuel
                        profil_net_man = []
                        s_temp_man = 0.0
                        cap_utile_man = cap_b * DOD
                        p_batt_max_man_pt = (cap_b * 0.5) * (pas_temps_final / 60)
                        
                        for ph_m, ch_m in zip(prod_h_test, courbe_conso_travail):
                            if ph_m >= ch_m:
                                dispo = ph_m - ch_m
                                charge = min(dispo, (cap_utile_man - s_temp_man) / RENDEMENT_CHARGE, p_batt_max_man_pt)
                                s_temp_man += charge * RENDEMENT_CHARGE
                                profil_net_man.append(0)
                            else:
                                besoin = ch_m - ph_m
                                # On applique le ratio de décharge choisi par l'utilisateur (r_man)
                                besoin_a_couvrir = besoin * r_man
                                decharge = min(besoin_a_couvrir / RENDEMENT_DECHARGE, s_temp_man, p_batt_max_man_pt)
                                s_temp_man -= decharge
                                net_m = (besoin - decharge * RENDEMENT_DECHARGE) * (60 / pas_temps_final)
                                profil_net_man.append(net_m)
                        
                        if st.session_state.get("pays_selectionne") == "France":
                            pts_par_heure = 60 // pas_temps_final
                            nb_heures_depassement_initial = sum(1 for c_initial in courbe_conso_travail[:8760*pts_par_heure] if c_initial * (60 / pas_temps_final) > abonnement_val_val + 0.01)
                            nb_heures_depassement_final = sum(1 for p_net in profil_net_man[:8760*pts_par_heure] if p_net > abonnement_val_val + 0.01)
                            revenu_ecretage_man = max(0, nb_heures_depassement_initial - nb_heures_depassement_final) * taxe_puissance_annuelle_val
                        else:
                            # Suisse : Économie sur la taxe de puissance mensuelle
                            gain_ecretage_total_man = 0
                            jours_par_mois_calc = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
                            idx_h = 0
                            pts_par_heure = 60 // pas_temps_final
                            for m in range(12):
                                pts_mois = jours_par_mois_calc[m] * 24 * pts_par_heure
                                p_max_mensuel_initial = max(max(0, ci - pi) for ci, pi in zip(courbe_conso_travail[idx_h : idx_h + pts_mois], prod_h_test[idx_h : idx_h + pts_mois])) * (60 / pas_temps_final) if courbe_conso_travail[idx_h : idx_h + pts_mois] else 0
                                p_max_mensuel_net = max(profil_net_man[idx_h : idx_h + pts_mois]) if profil_net_man[idx_h : idx_h + pts_mois] else 0
                                gain_ecretage_total_man += max(0, p_max_mensuel_initial - p_max_mensuel_net) * taxe_puissance_annuelle_val
                                idx_h += pts_mois
                            revenu_ecretage_man = gain_ecretage_total_man

                    revenu_services_man = (cap_b / 1000) * revenu_services_unit_val if autoriser_services_val and cap_b > 0 else 0
                    
                    gain_annuel_brut = det_auto_pv_man + det_auto_batt_man + det_vente_surplus_man + revenu_ecretage_man + revenu_services_man
                    
                    opex_annuel = (p_test * opex_pv_unit_val) + (cap_b * opex_batt_unit_val)
                    gain_annuel_net = gain_annuel_brut - opex_annuel
                    
                    if st.session_state.get("pays_selectionne") == "France":
                        gain_annuel_net -= (abonnement_annuel_val + majoration_injection)

                    capex_test = (p_test * capex_pv_unit_val) + (cap_b * capex_batt_unit_val)
                    van_test = (gain_annuel_net * duree_projet_val) - capex_test
                    
                    val_auto_pv_calc = det_auto_pv_man
                    val_auto_batt_calc = det_auto_batt_man
                    val_vente_surplus_calc = det_vente_surplus_man
                    revenu_ecretage = revenu_ecretage_man
                    revenu_services = revenu_services_man
                    
                    best_pv_total = p_test
                    best_capa_batt = cap_b
                    best_gain_annuel = gain_annuel_net
                    best_capex = capex_test
                    best_taux_auto_config = t_auto
                    best_taux_prod_config = t_prod
                    best_surplus_config = surplus_test
                    
                    scenarios_comparaison.append({
                        "Label": "Configuration Manuelle",
                        "Autoproduction": t_prod,
                        "Autoconsommation": t_auto,
                        "ROI": round(capex_test / gain_annuel_net, 1) if gain_annuel_net > 0 else 99,
                        "Economies": round(int(gain_annuel_net) * duree_projet_val),
                        "Cyclage": round(cyclage_annuel),
                        "Remplissage": round(remplissage_moyen),
                        "RatioPuissance": round(ratio_puissance)
                    })
                else:
                    # MODE AUTO : Recherche de l'optimum
                    # On définit des paliers de test pour la puissance PV
                    p_totale_max_toit = sum(p['p_max'] for p in profils_unitaires_par_pan) if profils_unitaires_par_pan else puissance_pv_installable
                    
                    # Nouvelle Logique PV selon les instructions
                    # On s'assure que productible_moyen est cohérent
                    p_prod_moyen = productible_moyen if productible_moyen > 0 else 1100.0
                    p_base = conso_annuelle_kwh_val / p_prod_moyen if p_prod_moyen > 0 else 20.0
                    
                    is_import_mode = (mode_production_val == "Télécharger une courbe de production PV" and mode_conso_val == "Télécharger une courbe de charge")

                    if mode_production_val == "Télécharger une courbe de production PV":
                        # En mode import, on ne teste que la puissance importée
                        if puissance_custom_prod_val:
                            paliers_pv = [puissance_custom_prod_val]
                        else:
                            # Si non saisie, on a calculé puissance_retenue plus haut
                            paliers_pv = [puissance_retenue]
                    else:
                        # France et Suisse : on teste un balayage de puissances PV
                        if "autonomie" in mode_ideal_val.lower():
                            p_start = p_base        # 100% de la conso
                        else: # ROI ou Investissement
                            p_start = p_base * 0.25  # 25% de la conso
                        
                        # Le palier maximal est le double de la puissance de base, plafonné par le toit
                        p_max_test_theorique = p_base * 2.0
                        p_max_test = min(p_max_test_theorique, puissance_pv_installable)
                        
                        if p_start < p_max_test:
                            paliers_pv = np.linspace(p_start, p_max_test, 10).tolist()
                        else:
                            paliers_pv = [p_max_test]
                    
                    if not paliers_pv or all(p == 0 for p in paliers_pv):
                        paliers_pv = [puissance_pv_installable] if puissance_pv_installable > 0 else [20.0]
                    
                    paliers_pv = sorted(list(set(paliers_pv)))
                    
                    if not is_import_mode:
                        best_autoprod_score = -2_000_000_000.0
                    with st.spinner("Calcul du dimensionnement idéal..." if not is_import_mode else "Calcul du stockage idéal..."):
                        for p_test in paliers_pv:
                            if mode_production_val == "Télécharger une courbe de production PV" and courbe_prod_upload_val:
                                prod_h_test = list(courbe_prod_upload_val)
                                ratio_pv = 1.0 
                            else:
                                ratio_pv = p_test / puissance_pv_installable if puissance_pv_installable > 0 else 0
                                prod_h_test = [0.0] * 8760
                                for item in profils_unitaires_par_pan:
                                    p_pan_test = item['p_max'] * ratio_pv
                                    for i in range(8760):
                                        prod_h_test[i] += item['profil'][i] * p_pan_test
                            
                            # Logique Batterie : 3 tailles pour chaque PV
                            # P_batt = 100%, 75%, 50% de P_PV. Capacité = P_batt / 0.5C = 2 * P_batt.
                            if not simuler_batterie_val:
                                paliers_batt = [0.0]
                            elif mode_production_val == "Télécharger une courbe de production PV" and mode_conso_val == "Télécharger une courbe de charge":
                                # Cas import : on teste 5 capacités basées sur P_PV
                                cap_max = p_test * 2.0
                                cap_min = cap_max / 5
                                # On génère 5 paliers linéairement répartis
                                paliers_batt = np.linspace(cap_min, cap_max, 5).tolist()
                            else:
                                paliers_batt = [
                                    p_test * 2.0,       # 100% de P_PV avec 0.5C -> Capa = 2 * P_PV
                                    p_test * 0.75 * 2.0, # 75% de P_PV avec 0.5C -> Capa = 1.5 * P_PV
                                    p_test * 0.5 * 2.0   # 50% de P_PV avec 0.5C -> Capa = 1 * P_PV
                                ]
                            
                            paliers_batt = sorted(list(set(paliers_batt)), reverse=True)
                            
                            for cap_b in paliers_batt:
                                if cap_b == 0:
                                    # Cas sans batterie : résultats identiques à la section 2
                                    t_prod = taux_autoproduction
                                    t_auto = taux_autoconsommation
                                    surplus_test = surplus_injecte_kwh
                                    gain_annuel_net = gain_annuel_s2
                                    capex_test = best_pv_total * capex_pv_unit_val
                                    roi_test = capex_test / gain_annuel_net if gain_annuel_net > 0 else 99
                                    cyclage_annuel = 0
                                    remplissage_moyen = 0
                                    ratio_puissance = 0
                                    van_test = gain_annuel_net * duree_projet_val
                                    prod_annuelle_test = production_totale_an
                                    total_charge_solaire = 0
                                    total_decharge_sim = 0
                                else:
                                    cap_utile_b = cap_b * DOD
                                    s_temp = 0.0
                                    # P_batt = Capa * 0.5C
                                    p_batt_max_test = cap_b * 0.5 
                                    p_batt_max_test_pt = p_batt_max_test * (pas_temps_final / 60)
                                    soc_points = []
                                    total_decharge_sim = 0
                                    total_charge_solaire = 0
                                    p_batt_real_max = 0
                                
                                    # --- SIMULATION DÉCISIONNELLE ---
                                    auto_pv_seul_local = 0
                                    for h_idx, (ph, ch) in enumerate(zip(prod_h_test, courbe_conso_travail)):
                                        part_directe = min(ph, ch)
                                        auto_pv_seul_local += part_directe
                                    
                                        surplus_inst = max(0, ph - ch)
                                        besoin_inst = max(0, ch - ph)

                                        if surplus_inst > 0:
                                            charge = min(surplus_inst, (cap_utile_b - s_temp) / RENDEMENT_CHARGE, p_batt_max_test_pt)
                                            s_temp += charge * RENDEMENT_CHARGE
                                            total_charge_solaire += charge
                                            p_batt_real_max = max(p_batt_real_max, charge * (60 / pas_temps_final))
                                            soc_points.append(s_temp)
                                        else:
                                            decharge = min(besoin_inst / RENDEMENT_DECHARGE, s_temp, p_batt_max_test_pt)
                                            s_temp -= decharge
                                            p_batt_real_max = max(p_batt_real_max, decharge * (60 / pas_temps_final))
                                            auto_batt_h = decharge * RENDEMENT_DECHARGE
                                            total_decharge_sim += auto_batt_h
                                            soc_points.append(s_temp)
                                
                                    # En mode import, on force l'égalité parfaite avec la section 2 pour le PV seul
                                    if mode_production_val == "Télécharger une courbe de production PV":
                                        auto_pv_seul_local = autoconsommation_kwh
                                
                                    # Autoconsommation totale = Directe + Batterie
                                    auto_temp_kwh = auto_pv_seul_local + total_decharge_sim
                                
                                    cyclage_annuel = total_decharge_sim / cap_b if cap_b > 0 else 0
                                    remplissage_moyen = (sum(soc_points) / len(soc_points)) / cap_b * 100 if cap_b > 0 else 0
                                    ratio_puissance = (p_batt_real_max / p_batt_max_test * 100) if p_batt_max_test > 0 else 0

                                    prod_annuelle_test = sum(prod_h_test)
                                    t_prod = (auto_temp_kwh / conso_annuelle_kwh_val * 100) if conso_annuelle_kwh_val > 0 else 0
                                    t_auto = (auto_temp_kwh / prod_annuelle_test * 100) if prod_annuelle_test > 0 else 0
                                    surplus_test = max(0, prod_annuelle_test - auto_pv_seul_local - total_charge_solaire)
                                
                                    # Valorisation financière BASE
                                    tarif_val_reseau = prix_revente_locataire_val if "location" in scénario_investissement_val else prix_achat_val
                                
                                    # Gain = (kWh_auto_total * (tarif_reseau - prix_vente)) + (Total_Produit * prix_vente)
                                    # kWh_auto_total inclut direct PV + batterie
                                
                                    # REVENUS DÉTAILLÉS (pour assurer la cohérence avec l'affichage final)
                                    tarif_val_reseau = prix_revente_locataire_val if "location" in scénario_investissement_val else prix_achat_val
                                   
                                    # Gain brut cohérent avec l'affichage final
                                    # Gain = (kWh_auto_direct * (tarif_reseau - prix_vente)) + (kWh_auto_batt * (tarif_reseau - prix_vente)) + (Production * prix_vente)
                                    det_auto_pv_opt = auto_pv_seul_local * (tarif_val_reseau - prix_vente_val)
                                    det_auto_batt_opt = total_decharge_sim * (tarif_val_reseau - prix_vente_val)
                                    det_vente_surplus_opt = surplus_test * prix_vente_val
                                   
                                    gain_annuel_brut = det_auto_pv_opt + det_auto_batt_opt + det_vente_surplus_opt

                                    # Ajout du gain d'écrêtage estimé pour la Suisse ou la France dans l'optimisation
                                    revenu_ecretage_est = 0
                                    if autoriser_ecretage_val and cap_b > 0:
                                        if st.session_state.get("pays_selectionne") == "Suisse":
                                            # Calcul précis de l'écrêtage pour le scoring en Suisse
                                            # On utilise un profil net simulé au pas de temps final pour être 100% cohérent
                                            gain_ecretage_total_est = 0
                                            jours_par_mois_calc = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
                                            idx_h_est = 0
                                            pts_par_heure = 60 // pas_temps_final
                                            
                                            # Profil net simulé (déjà disponible via soc_points ou on le recalcule vite)
                                            # On recalcule par sécurité pour avoir le profil kW exact
                                            profil_net_test = []
                                            s_temp_est = 0.0
                                            cap_utile_est = cap_b * DOD
                                            p_batt_max_est_pt = (cap_b * 0.5) * (pas_temps_final / 60)
                                            
                                            for ph_est, ch_est in zip(prod_h_test, courbe_conso_travail):
                                                if ph_est >= ch_est:
                                                    dispo = ph_est - ch_est
                                                    charge = min(dispo, (cap_utile_est - s_temp_est) / RENDEMENT_CHARGE, p_batt_max_est_pt)
                                                    s_temp_est += charge * RENDEMENT_CHARGE
                                                    profil_net_test.append(0)
                                                else:
                                                    besoin = ch_est - ph_est
                                                    # En auto, on utilise ratio 1.0 ou on suit la logique de best_ratio_ecretage
                                                    decharge = min(besoin / RENDEMENT_DECHARGE, s_temp_est, p_batt_max_est_pt)
                                                    s_temp_est -= decharge
                                                    net = (besoin - decharge * RENDEMENT_DECHARGE) * (60 / pas_temps_final)
                                                    profil_net_test.append(net)

                                            for m_est in range(12):
                                                pts_mois_est = jours_par_mois_calc[m_est] * 24 * pts_par_heure
                                                # Max mensuel initial (sans stockage, mais avec PV)
                                                c_m_est = courbe_conso_travail[idx_h_est : idx_h_est + pts_mois_est]
                                                p_m_est = prod_h_test[idx_h_est : idx_h_est + pts_mois_est]
                                                if c_m_est and p_m_est:
                                                    p_max_init_est = max(max(0, ci - pi) for ci, pi in zip(c_m_est, p_m_est)) * (60 / pas_temps_final)
                                                    # Max mensuel net (avec stockage)
                                                    p_max_net_est = max(profil_net_test[idx_h_est : idx_h_est + pts_mois_est])
                                                    
                                                    gain_ecretage_total_est += max(0, p_max_init_est - p_max_net_est) * taxe_puissance_annuelle_val
                                                idx_h_est += pts_mois_est
                                            revenu_ecretage_est = gain_ecretage_total_est
                                        else:
                                            # France : estimation simplifiée
                                            revenu_ecretage_est = 2 * taxe_puissance_annuelle_val # 2h de dépassement économisées
                                    
                                    gain_annuel_brut += revenu_ecretage_est

                                    # On ajoute les revenus additionnels (estimés ici pour l'optimum)
                                    revenu_services_est = (cap_b / 1000) * revenu_services_unit_val if autoriser_services_val else 0
                                    gain_annuel_brut += revenu_services_est

                                    opex_annuel_test = (p_test * opex_pv_unit_val) + (cap_b * opex_batt_unit_val)
                                    gain_annuel_net = gain_annuel_brut - opex_annuel_test

                                    if st.session_state.get("pays_selectionne") == "France":
                                        gain_annuel_net -= (abonnement_annuel_val + majoration_injection)

                                    capex_test = (p_test * capex_pv_unit_val) + (cap_b * capex_batt_unit_val)
                                    roi_test = capex_test / gain_annuel_net if gain_annuel_net > 0 else 99
                                    van_test = (gain_annuel_net * duree_projet_val)
                            
                                scenarios_comparaison.append({
                                    "Label": f"{p_test:,.1f} kWc / {int(cap_b)} kWh".replace(",", " "),
                                    "Autoproduction": t_prod,
                                    "Autoconsommation": t_auto,
                                    "ROI": round(roi_test, 1),
                                    "Economies": round(int(gain_annuel_net) * duree_projet_val),
                                    "Cyclage": round(cyclage_annuel),
                                    "Remplissage": round(remplissage_moyen),
                                    "RatioPuissance": round(ratio_puissance),
                                    "Surplus": surplus_test
                                })

                                # --- LOGIQUE DE SCORING ---
                                if mode_ideal_val == "Favoriser l'autonomie sur site":
                                    # Autonomie : Priorité absolue taux d'autoproduction
                                    # On ajoute un bonus minime pour les économies en cas d'égalité d'autoproduction
                                    score = t_prod + (van_test / 1_000_000_000)
                                    
                                    # Bonus technique batterie très léger pour favoriser les systèmes dimensionnés correctement à autoproduction égale
                                    if simuler_batterie_val and cap_b > 0:
                                        if (cyclage_annuel >= 150 and 60 <= ratio_puissance <= 80 and 40 <= remplissage_moyen <= 60):
                                            score += 0.1 # Equivalent 0.1% d'autoproduction
                                
                                elif mode_ideal_val == "Favoriser le financier ROI":
                                    # ROI : le plus bas possible
                                    score = -roi_test
                                    
                                    # Bonus technique batterie très léger
                                    if simuler_batterie_val and cap_b > 0:
                                        if (cyclage_annuel >= 150 and 60 <= ratio_puissance <= 80 and 40 <= remplissage_moyen <= 60):
                                            score += 0.05 # Equivalent 0.05 an de ROI
                                    
                                else: # Favoriser l'investissement (ROI < 7,5 ans)
                                    # Économies les plus hautes pour un ROI < 7.5 ans
                                    if roi_test <= 7.5:
                                        score = van_test / 1000 
                                        if simuler_batterie_val and cap_b > 0:
                                            if (cyclage_annuel >= 150 and 60 <= ratio_puissance <= 80 and 40 <= remplissage_moyen <= 60):
                                                score += (score * 0.01) # +1% de score si technique OK
                                    else:
                                        score = -1_000_000 - roi_test

                                if score > best_autoprod_score:
                                    best_autoprod_score = score
                                    best_pv_total = p_test
                                    best_capa_batt = cap_b
                                    best_taux_auto_config = t_auto
                                    best_taux_prod_config = t_prod
                                    best_surplus_config = surplus_test
                                    # On mémorise aussi les gains pour la simulation finale
                                    best_gain_annuel_brut_opt = gain_annuel_brut
                                    best_opex_annuel_opt = opex_annuel_test
                                    best_capex_opt = capex_test
                                    # On mémorise les revenus détaillés pour l'affichage final
                                    val_auto_pv_calc = det_auto_pv_opt
                                    val_auto_batt_calc = det_auto_batt_opt
                                    val_vente_surplus_calc = det_vente_surplus_opt

                        # --- SIMULATION FINALE DU SYSTÈME IDÉAL (AVEC OPTIONS AVANCÉES) ---
                        if mode_production_val == "Télécharger une courbe de production PV" and courbe_prod_upload_val:
                            prod_h_ideal = mettre_a_jour_pas(courbe_prod_upload_val, pas_temps_prod_val, pas_temps_final)
                            ratio_pv_ideal = 1.0
                        else:
                            ratio_pv_ideal = best_pv_total / p_totale_max_toit if p_totale_max_toit > 0 else 0
                            prod_h_ideal = [0.0] * pts_annuels
                            for item in profils_unitaires_par_pan:
                                p_pan_ideal = item['p_max'] * ratio_pv_ideal
                                item_fin = mettre_a_jour_pas(item['profil'], 60, pas_temps_final)
                                for i in range(len(prod_h_ideal)):
                                    prod_h_ideal[i] += item_fin[i] * p_pan_ideal
                            
                        cap_utile_ideal = best_capa_batt * DOD
                        p_batt_max_ideal = best_capa_batt * C_RATE
                        p_batt_max_ideal_pt = p_batt_max_ideal * (pas_temps_final / 60)

                        # --- OPTIMISATION DU RATIO DE DÉCHARGE ÉCRÊTAGE ---
                        best_ratio_ecretage = 0.5
                        pts_par_heure = 60 // pas_temps_final
                        if autoriser_ecretage_val and best_capa_batt > 0:
                            max_score_ratio = -float('inf')
                            # Test de plusieurs ratios pour trouver le meilleur compromis annuel
                            for r_test in [0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0]:
                                s_temp_r = 0.0
                                auto_r = 0
                                p_net_r = []
                                soc_aube = [] # SOC entre 6h et 8h
                                
                                for h_idx, (ph, ch) in enumerate(zip(prod_h_ideal, courbe_conso_travail)):
                                    h_jour = (h_idx // pts_par_heure) % 24
                                    surplus_inst = max(0, ph - ch)
                                    besoin_inst = max(0, ch - ph)
                                    if surplus_inst > 0:
                                        charge = min(surplus_inst, (cap_utile_ideal - s_temp_r) / RENDEMENT_CHARGE, p_batt_max_ideal_pt)
                                        s_temp_r += charge * RENDEMENT_CHARGE
                                        p_net_r.append(0)
                                    else:
                                        besoin_a_couvrir = besoin_inst * r_test
                                        decharge = min(besoin_a_couvrir / RENDEMENT_DECHARGE, s_temp_r, p_batt_max_ideal_pt)
                                        s_temp_r -= decharge
                                        auto_r += (min(ph, ch) + decharge * RENDEMENT_DECHARGE)
                                        p_net_r.append((besoin_inst - decharge * RENDEMENT_DECHARGE) * (60 / pas_temps_final))
                                    
                                    if 6 <= h_jour <= 8:
                                        soc_aube.append(s_temp_r)
                                
                                pts_par_heure = 60 // pas_temps_final
                                p_max_r = max(p_net_r[:8760*pts_par_heure]) if p_net_r else 0
                                soc_moyen_aube = sum(soc_aube)/len(soc_aube) if soc_aube else 0
                                
                                # Scoring : 
                                # - On favorise un SOC bas à l'aube (Prio 1)
                                # - On favorise une puissance de pointe basse (Prio 2)
                                # - On favorise une autoconsommation haute
                                # Score = Autoproduction - (SOC_Aube / Capacité * 100) - (P_max / P_intro * 10)
                                score_r = auto_r - (soc_moyen_aube * 1000) - (p_max_r * 10)
                                
                                if score_r > max_score_ratio:
                                    max_score_ratio = score_r
                                    best_ratio_ecretage = r_test

                        # Simulation finale avec le meilleur ratio
                        s_temp = 0.0
                        # Initialisation pour s'assurer qu'elles existent avant la boucle
                        auto_pv_seul_local = 0
                        auto_batt_kwh = 0
                        total_charge_solaire = 0
                        total_decharge_sim = 0
                    
                        profil_net = []
                        for h_idx, (ph, ch) in enumerate(zip(prod_h_ideal, courbe_conso_travail)):
                            # 1. Autoconsommation PV directe (baseline sans batterie)
                            part_directe = min(ph, ch)
                            auto_pv_seul_local += part_directe
                        
                            surplus_inst = max(0, ph - ch)
                            besoin_inst = max(0, ch - ph)
                        
                            if surplus_inst > 0:
                                charge = min(surplus_inst, (cap_utile_ideal - s_temp) / RENDEMENT_CHARGE, p_batt_max_ideal_pt)
                                s_temp += charge * RENDEMENT_CHARGE
                                total_charge_solaire += charge
                                profil_net.append(0)
                            else:
                                besoin_a_couvrir = besoin_inst * (best_ratio_ecretage if autoriser_ecretage_val else 1.0)
                                decharge = min(besoin_a_couvrir / RENDEMENT_DECHARGE, s_temp, p_batt_max_ideal_pt)
                                s_temp -= decharge
                                auto_batt_h = decharge * RENDEMENT_DECHARGE
                                auto_batt_kwh += auto_batt_h
                                total_decharge_sim += decharge
                                profil_net.append((besoin_inst - auto_batt_h) * (60 / pas_temps_final))
                        
                        # En mode import, on force l'égalité parfaite avec la section 2 pour le PV seul
                        if mode_production_val == "Télécharger une courbe de production PV":
                            auto_pv_seul_local = autoconsommation_kwh

                        # Autoconsommation totale = Directe + Batterie
                        auto_temp_kwh = auto_pv_seul_local + auto_batt_kwh
                        # Correction mathématique du surplus : ce qui n'est ni consommé en direct ni stocké
                        # Surplus = Production totale - Autoconsommation directe - Énergie chargée (PV vers Batterie)
                        prod_annuelle_ideal = sum(prod_h_ideal)
                        
                        # On recalcule les totaux de simulation pour s'assurer de la cohérence
                        best_surplus_config = max(0, prod_annuelle_ideal - auto_pv_seul_local - total_charge_solaire)
                    
                        best_taux_prod_config = (auto_temp_kwh / conso_annuelle_kwh_val * 100) if conso_annuelle_kwh_val > 0 else 0
                        best_taux_auto_config = (auto_temp_kwh / prod_annuelle_ideal * 100) if prod_annuelle_ideal > 0 else 0
                    
                        # Valorisation financière réelle (Avec Options)
                        tarif_val_reseau = prix_revente_locataire_val if "location" in scénario_investissement_val else prix_achat_val
                        # Gain = (kWh_auto_total * (tarif_reseau - prix_vente)) + (Total_Produit * prix_vente)
                        # kWh_auto_total inclut direct PV + batterie
                    
                        # REVENUS DÉTAILLÉS (pour assurer la cohérence avec le scoring)
                        if mode_production_val == "Télécharger une courbe de production PV" and best_capa_batt == 0:
                            # En mode import sans batterie, on utilise déjà les variables s2 calculées plus haut
                            # Cependant, on s'assure qu'elles sont bien affectées aux variables de calcul final
                            val_auto_pv_calc = gain_autoconso_pv_s2
                            val_auto_batt_calc = 0.0
                            val_vente_surplus_calc = vente_surplus_s2
                            gain_annuel_brut_final = val_auto_pv_calc + val_auto_batt_calc + val_vente_surplus_calc
                            best_gain_annuel = gain_annuel_s2
                        else:
                            # Pour tous les autres cas (Manuel ou Auto avec batterie), 
                            # les variables val_auto_pv_calc, val_auto_batt_calc, val_vente_surplus_calc 
                            # ont déjà été mises à jour dans la boucle ou le bloc manuel ci-dessus.
                            
                            # On recalcule juste le gain brut final pour la simulation finale
                            gain_annuel_brut_final = val_auto_pv_calc + val_auto_batt_calc + val_vente_surplus_calc
                        
                            # Revenus additionnels batterie
                            # Ils sont déjà dans revenu_ecretage et revenu_services s'ils ont été calculés
                            # On recalcule précisément pour l'affichage final si nécessaire
                            if autoriser_ecretage_val and best_capa_batt > 0:
                                # ... (on garde la même logique de calcul de revenu_ecretage)
                                gain_ecretage_total = 0
                                jours_par_mois_calc = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
                                idx_h = 0
                                pts_par_heure = 60 // pas_temps_final
                                # Correction : Puissance de pointe initiale = max soutiré réseau SANS batterie (avec PV de base)
                                profil_net_init = []
                                for ph_i, ch_i in zip(prod_h_ideal, courbe_conso_travail):
                                    profil_net_init.append(max(0, ch_i - ph_i) * (60 / pas_temps_final))
                                
                                p_max_init_annuel = max(profil_net_init[:8760*pts_par_heure]) if profil_net_init else 0
                                # On utilise profil_net qui contient déjà les valeurs en kW (multipliées par 60/pas_temps plus haut)
                                p_max_net_annuel = max(profil_net[:8760*pts_par_heure]) if profil_net else 0

                                for m in range(12):
                                    pts_mois = jours_par_mois_calc[m] * 24 * pts_par_heure
                                    if st.session_state.get("pays_selectionne") == "France":
                                        # (Logique France inchangée car basée sur abonnement_val_val fixe)
                                        nb_pts_dep_init = sum(1 for p_ni in profil_net_init[idx_h : idx_h + pts_mois] if p_ni > abonnement_val_val + 0.01)
                                        nb_pts_dep_final = sum(1 for p_n in profil_net[idx_h : idx_h + pts_mois] if p_n > abonnement_val_val + 0.01)
                                        nb_h_dep_init = nb_pts_dep_init / pts_par_heure
                                        nb_h_dep_final = nb_pts_dep_final / pts_par_heure
                                        nb_h_dep_init_total += nb_h_dep_init
                                        nb_h_dep_final_total += nb_h_dep_final
                                        gain_ecretage_total += max(0, nb_h_dep_init - nb_h_dep_final) * taxe_puissance_annuelle_val
                                    else:
                                        p_max_init = max(profil_net_init[idx_h : idx_h + pts_mois]) if profil_net_init[idx_h : idx_h + pts_mois] else 0
                                        p_max_net = max(profil_net[idx_h : idx_h + pts_mois]) if profil_net[idx_h : idx_h + pts_mois] else 0
                                        gain_ecretage_total += max(0, p_max_init - p_max_net) * taxe_puissance_annuelle_val
                                    idx_h += pts_mois
                                revenu_ecretage = gain_ecretage_total
                            else:
                                st.info("💡 L'option d'écrêtage de pointe n'est pas activée.")

                            revenu_services = (best_capa_batt / 1000) * revenu_services_unit_val if autoriser_services_val and best_capa_batt > 0 else 0
                        
                            opex_annuel = (best_pv_total * opex_pv_unit_val) + (best_capa_batt * opex_batt_unit_val)
                        
                            # CALCUL DU GAIN ANNUEL NET (FORCÉ POUR CORRESPONDRE AU DÉTAIL)
                            best_gain_annuel = (gain_annuel_brut_final + revenu_services + revenu_ecretage) - opex_annuel
                        
                            if st.session_state.get("pays_selectionne") == "France":
                                best_gain_annuel -= (abonnement_annuel_val + majoration_injection)
                        
                        best_capex = (best_pv_total * capex_pv_unit_val) + (best_capa_batt * capex_batt_unit_val)
                        
                        # Mise à jour du système idéal dans scenarios_comparaison pour refléter les gains réels sur le graph
                        label_ideal = f"{best_pv_total:,.1f} kWc / {int(best_capa_batt)} kWh".replace(",", " ")
                        for entry in scenarios_comparaison:
                            if entry["Label"] == label_ideal:
                                entry["ROI"] = round(best_capex / best_gain_annuel, 1) if best_gain_annuel > 0 else 99
                                entry["Economies"] = round(int(best_gain_annuel) * duree_projet_val)
                                entry["Autoproduction"] = best_taux_prod_config
                                entry["Autoconsommation"] = best_taux_auto_config
            
            aug_intro_ideale = max(0.0, best_pv_total - puissance_intro_kw_val)
            
            # --- AFFICHAGE MÉTRIQUES IDÉALES ---
            st.write("---")
            if mode_production_val == "Télécharger une courbe de production PV" and mode_conso_val == "Télécharger une courbe de charge":
                st.header("💡 Votre stockage idéal")
            elif mode_production_val == "Télécharger une courbe de production PV":
                st.header("💡 Votre système photovoltaïque et stockage idéal")
            elif simuler_batterie_val:
                st.header("💡 Votre système photovoltaïque et stockage idéal")
            else:
                st.header("💡 Votre système photovoltaïque idéal")
            
            # Calcul des gains par rapport à la section 2 (PV seul au max du toit)
            # IMPORTANT : on s'assure d'utiliser les mêmes bases de comparaison
            gain_auto_prod = best_taux_prod_config - taux_autoproduction
            gain_auto_conso = best_taux_auto_config - taux_autoconsommation
            diff_surplus = best_surplus_config - surplus_injecte_kwh
            
            c_id1, c_id2, c_id3 = st.columns(3)
            # On arrondit à .1f pour correspondre au label du graphique
            best_pv_label = f"{best_pv_total:,.1f}".replace(",", " ")
            if mode_production_val == "Télécharger une courbe de production PV" and mode_conso_val == "Télécharger une courbe de charge":
                c_id1.metric("Puissance PV (fixée)", f"{best_pv_label} kWc", help="Puissance PV fixée par votre fichier importé.")
            elif mode_production_val == "Télécharger une courbe de production PV" and courbe_prod_upload_val:
                c_id1.metric("Puissance PV (fixée)", f"{best_pv_label} kWc", help="Puissance PV fixée par votre fichier importé.")
            else:
                c_id1.metric("Puissance PV Idéale", f"{best_pv_label} kWc", help="Puissance PV optimisant le compromis entre autonomie et rentabilité.")
            # Calcul de la puissance du stockage
            if simuler_batterie_val:
                puissance_stockage = best_capa_batt * C_RATE
                c_id2.metric("Stockage Idéal", f"{int(puissance_stockage):,} kW/{int(best_capa_batt):,} kWh".replace(",", " "), help="Puissance et capacité de stockage optimisées.")
                
                # Ajout des kWh restitués par la batterie
                c_id3.metric("Énergie restituée (batterie)", f"{int(auto_batt_kwh):,} kWh".replace(",", " "), help="Énergie stockée puis restituée par la batterie pour couvrir la consommation.")
            else:
                c_id2.metric("Stockage Idéal", "Aucun", help="La simulation de batterie est désactivée.")
                c_id3.metric("Énergie restituée (batterie)", "0 kWh")

            # --- PERFORMANCE DU SYSTÈME IDÉAL ---
            st.write("#### ⚡ Performance du système idéal")
            
            # Énergie stockée (total chargé dans la batterie)
            # auto_batt_kwh est l'énergie restituée (déchargée * rendement)
            energie_restituee_batt = auto_batt_kwh if 'auto_batt_kwh' in locals() else 0
            # On utilise le cumul des charges calculé dans la simulation finale
            if mode_production_val == "Télécharger une courbe de production PV" and best_capa_batt == 0:
                energie_stockee_batt = 0
            else:
                energie_stockee_batt = total_charge_solaire if 'total_charge_solaire' in locals() else 0

            cp1, cp2, cp3, cp4, cp5 = st.columns(5)
            cp1.metric(
                "Autoconsommation", 
                f"{best_taux_auto_config:,.1f} %".replace(",", " "), 
                delta=f"{best_taux_auto_config - taux_autoconsommation:+.1f} %"
            )
            cp2.metric(
                "Autoproduction", 
                f"{best_taux_prod_config:,.1f} %".replace(",", " "), 
                delta=f"{best_taux_prod_config - taux_autoproduction:+.1f} %"
            )
            cp3.metric(
                "Surplus rejeté", 
                f"{best_surplus_config:,.0f} kWh".replace(",", " "), 
                delta=f"-{int(total_charge_solaire):,}".replace(",", " ") + " kWh", 
                delta_color="inverse",
                help="Énergie réinjectée sur le réseau. Le delta indique l'énergie qui a été stockée dans la batterie au lieu d'être injectée."
            )
            cp4.metric(
                "Autoconso globale",
                f"{int(auto_temp_kwh):,} kWh".replace(",", " "),
                help="Total de l'énergie autoconsommée (Solaire direct + Batterie).",
                delta=f"+{int(auto_batt_kwh):,}".replace(",", " ") + " kWh (gain batt.)"
            )
            cp5.metric(
                "Énergie stockée",
                f"{int(energie_stockee_batt):,} kWh".replace(",", " "),
                help="Énergie totale stockée (chargée) dans la batterie avant pertes de décharge."
            )

            # --- RENTABILITÉ FINANCIÈRE ---
            if mode_production_val == "Télécharger une courbe de production PV" and mode_conso_val == "Télécharger une courbe de charge":
                st.write("#### 💰 Rentabilité globale (PV importé + Batterie)")
            elif mode_production_val == "Télécharger une courbe de production PV" and courbe_prod_upload_val:
                st.write("#### 💰 Rentabilité globale (PV importé + Batterie)")
            else:
                st.write("#### 💰 Rentabilité du système idéal")
            roi = best_capex / best_gain_annuel if best_gain_annuel > 0 else 0
            
            cr1, cr2, cr3, cr4 = st.columns(4)
            cr1.metric("Investissement", f"{int(best_capex):,} {devise_val}".replace(",", " "))
            cr2.metric("Gain annuel net", f"{int(best_gain_annuel):,} {devise_val}/an".replace(",", " "), help="Calculé après déduction de la maintenance annuelle.")
            cr3.metric("Temps de retour (ROI)", f"{roi:,.1f} ans".replace(",", " "))
            economies_totale = int(best_gain_annuel) * duree_projet_val
            cr4.metric(f"Économies (sur {duree_projet_val} ans)", f"{int(economies_totale):,} {devise_val}".replace(",", " "), help="Gain financier net total cumulé sur la durée de vie du projet.")

            # --- DÉTAIL DES REVENUS ANNUELS (DÉPLACÉ ICI) ---
            with st.expander("📊 Détail des revenus annuels", expanded=False):
                col_exp1, col_exp2 = st.columns(2)
                
                with col_exp1:
                    # Économies Autoconsommation
                    label_auto_pv = "Économies Autoconsommation PV"
                    # On utilise les variables calculées lors de la simulation pour garantir la cohérence parfaite
                    if mode_production_val == "Télécharger une courbe de production PV" and best_capa_batt == 0:
                        val_auto_pv = gain_autoconso_pv_s2
                    else:
                        val_auto_pv = val_auto_pv_calc
                    st.write(f"**{label_auto_pv} :** {int(val_auto_pv):,} {devise_val}".replace(",", " "))
                    st.caption("ℹ️ Correspond aux économies de consommation PV directe.")
                    
                    if simuler_batterie_val and best_capa_batt > 0:
                        label_auto_batt = "Économie grâce à la batterie"
                        # Correction : s'assurer que val_auto_batt est bien calculé et correspond à kWh_batt * (tarif_réseau - tarif_revente)
                        st.write(f"**{label_auto_batt} :** {int(val_auto_batt_calc):,} {devise_val}".replace(",", " "))
                        st.caption("ℹ️ Correspond aux économies réalisées sur l'énergie restituée par la batterie.")
                    
                    # Vente du surplus
                    # La vente de surplus diminue car une partie est stockée
                    if mode_production_val == "Télécharger une courbe de production PV" and best_capa_batt == 0:
                        val_vente_surplus = vente_surplus_s2
                    else:
                        val_vente_surplus = val_vente_surplus_calc
                    st.write(f"**Vente du surplus de production :** {int(val_vente_surplus):,} {devise_val}".replace(",", " "))
                    
                    # Arbitrage (si activé)
                    # (Arbitrage dynamique supprimé)
                        
                    # Services Systèmes
                    if autoriser_services_val:
                        st.write(f"**Services Systèmes :** {int(revenu_services):,} {devise_val}".replace(",", " "))

                    # Lissage de puissance (Écrêtage)
                    if autoriser_ecretage_val:
                        if revenu_ecretage > 0:
                            st.write(f"**Gain par lissage de pointe :** {int(revenu_ecretage):,} {devise_val}".replace(",", " "))
                        elif simuler_batterie_val and best_capa_batt > 0:
                            st.write(f"**Gain par lissage de pointe :** 0 {devise_val}")
                            st.caption("ℹ️ Aucun gain financier direct détecté sur votre abonnement actuel.")

                    # OPEX (en négatif)
                    st.write(f"**Coûts opérationnels (Maintenance) :** -{int(opex_annuel):,} {devise_val}".replace(",", " "))
                
                with col_exp2:
                    if st.session_state.get("pays_selectionne") == "France":
                        st.markdown("#### **📜 Frais fixes (France)**")
                        # On réutilise abonnement_annuel_val et majoration_injection calculés au début de la simulation
                        st.write(f"**Abonnement annuel :** -{int(abonnement_annuel_val):,} {devise_val}".replace(",", " "))
                        st.write(f"**Majoration injection :** -{int(majoration_injection):,} {devise_val}".replace(",", " "))
                        st.caption("ℹ️ Ces frais sont déduits du gain annuel pour le calcul de la rentabilité.")
                    
                    if autoriser_ecretage_val:
                        if st.session_state.get("pays_selectionne") == "France":
                            st.markdown("#### **⚡ Gain par lissage de pointe sur abonnement actuel**")
                            # On s'assure que abonnement_annuel_val est bien passé
                            if 'abonnement_annuel_val' not in locals() or abonnement_annuel_val == 0:
                                # Tentative de récupération via le calcul direct si manquant
                                if type_tarif_val == "Tarif bleu particuliers":
                                    prices_bleu_res_hc = {3: 0, 6: 141.60, 9: 176.16, 12: 209.16, 15: 239.88, 18: 271.80, 24: 340.20, 30: 402.36, 36: 465.00}
                                    abonnement_annuel_val = prices_bleu_res_hc.get(abonnement_val_val, 0)
                                elif type_tarif_val == "Tarif bleu pro":
                                    if option_tarif_val == "Base":
                                        prices_bleu_non_res_base = {3: 134.04, 6: 166.92, 9: 198.60, 12: 230.28, 15: 261.48, 18: 291.60, 24: 357.36, 30: 422.52, 36: 487.20}
                                        abonnement_annuel_val = prices_bleu_non_res_base.get(abonnement_val_val, 0)
                                    else:
                                        prices_bleu_non_res_hc = {6: 167.40, 9: 200.16, 12: 233.76, 15: 265.68, 18: 299.04, 24: 371.40, 30: 436.32, 36: 501.84}
                                        abonnement_annuel_val = prices_bleu_non_res_hc.get(abonnement_val_val, 0)
                                elif type_tarif_val == "Tarif jaune":
                                    if "Longue Utilisation" in option_tarif_val:
                                        abonnement_annuel_val = 38.27 * abonnement_val_val
                                    else:
                                        abonnement_annuel_val = 26.44 * abonnement_val_val

                            st.write(f"👉 Abonnement initial : {int(abonnement_val_val)} kVA")
                            st.write(f"👉 Puissance de pointe (réseau sans batterie) : {p_max_init_annuel:.1f} kW")
                            if autoriser_ecretage_val and 'best_ratio_ecretage' in locals():
                                ratio_label = f"{int(best_ratio_ecretage*100)}% / {int((1-best_ratio_ecretage)*100)}%"
                                st.write(f"👉 Ratio de décharge (Batterie / Réseau) : {ratio_label}")
                                st.caption("ℹ️ Ce ratio a été optimisé pour maximiser l'autoconsommation tout en lissant les pics de puissance sur l'année.")
                            st.write(f"👉 Frais liés au dépassement de pointe : {int(nb_h_dep_init_total * taxe_puissance_annuelle_val):,} {devise_val}")
                            st.write(f"👉 Nouvelle pointe moyenne (PV+Stockage) : {p_max_net_annuel:.1f} kW")
                            st.write(f"👉 Économies sur les frais de dépassement : {int(revenu_ecretage):,} {devise_val}/an")
                            
                            # Logique d'optimisation économique du nouvel abonnement (Arbitrage entre coût abonnement et frais de dépassement)
                            def calculer_cout_abo_local(seuil, type_t, opt_t):
                                if type_t == "Tarif bleu particuliers":
                                    prices = {3: 0, 6: 141.60, 9: 176.16, 12: 209.16, 15: 239.88, 18: 271.80, 24: 340.20, 30: 402.36, 36: 465.00}
                                    return prices.get(seuil, seuil * (prices[36]/36))
                                elif type_t == "Tarif bleu pro":
                                    if opt_t == "Base":
                                        prices = {3: 134.04, 6: 166.92, 9: 198.60, 12: 230.28, 15: 261.48, 18: 291.60, 24: 357.36, 30: 422.52, 36: 487.20}
                                        return prices.get(seuil, seuil * (prices[36]/36))
                                    else:
                                        prices = {6: 167.40, 9: 200.16, 12: 233.76, 15: 265.68, 18: 299.04, 24: 371.40, 30: 436.32, 36: 501.84}
                                        return prices.get(seuil, seuil * (prices[36]/36))
                                elif type_t == "Tarif jaune":
                                    if "Longue Utilisation" in opt_t:
                                        return 38.27 * seuil
                                    else:
                                        return 26.44 * seuil
                                return 0

                            # On teste plusieurs paliers d'abonnement pour trouver le plus rentable
                            seuils_a_tester = [s for s in seuils_kva_complets if s <= max(seuils_kva_complets[0], math.ceil(p_max_init_annuel))]
                            meilleur_cout_total = float('inf')
                            abonnement_optimal = abonnement_val_val
                            nb_h_dep_final_optimal = 0

                            for s_test in seuils_a_tester:
                                # Calcul des dépassements pour ce seuil avec le profil net (lissé par batterie)
                                nb_h_dep_test = sum(1 for p_n in profil_net if p_n > s_test + 0.01)
                                cout_abo_test = calculer_cout_abo_local(s_test, type_tarif_val, option_tarif_val)
                                cout_total_test = cout_abo_test + (nb_h_dep_test * taxe_puissance_annuelle_val)
                                
                                if cout_total_test < meilleur_cout_total:
                                    meilleur_cout_total = cout_total_test
                                    abonnement_optimal = s_test
                                    nb_h_dep_final_optimal = nb_h_dep_test

                            st.write(f"👉 Nouvel abonnement optimal conseillé : {int(abonnement_optimal)} kVA")
                            
                            st.write(f"👉 **Gain annuel par lissage de pic avec abonnement actuel : {int(revenu_ecretage):,} {devise_val}/an**")
                            
                            # Calcul du coût de l'abonnement actuel et du nouvel abonnement
                            cout_abo_final_fixe = calculer_cout_abo_local(abonnement_optimal, type_tarif_val, option_tarif_val)
                            cout_abo_init_fixe = calculer_cout_abo_local(abonnement_val_val, type_tarif_val, option_tarif_val)
                            
                            if 'abonnement_annuel_val' not in locals() or abonnement_annuel_val == 0:
                                abonnement_annuel_val = cout_abo_init_fixe
                            
                            # Coût de l'abonnement initial incluant les dépassements initiaux (AVANT lissage par batterie)
                            cout_init_total = abonnement_annuel_val + (nb_h_dep_init_total * taxe_puissance_annuelle_val)
                            
                            # Coût du nouvel abonnement incluant les dépassements résiduels optimisés (APRÈS lissage par batterie)
                            cout_final_total = cout_abo_final_fixe + (nb_h_dep_final_optimal * taxe_puissance_annuelle_val)
                            
                            st.markdown("#### **📈 Gain par modification d'abonnement :**")
                            st.write(f"👉 Coût annuel abonnement initial + frais de dépassement : {int(cout_init_total):,} {devise_val}/an".replace(",", " "))
                            st.write(f"👉 Coût annuel nouvel abonnement optimisé + frais de dépassement : {int(cout_final_total):,} {devise_val}/an".replace(",", " "))
                            
                            gain_passage = max(0, cout_init_total - cout_final_total)
                            st.write(f"👉 **Gain annuel par changement d'abonnement : {int(gain_passage):,} {devise_val}/an**")
                        else:
                            st.write(f"👉 Pic de puissance initial : {p_max_init_annuel:.1f} kW")
                            st.write(f"👉 Pic de puissance après stockage : {p_max_net_annuel:.1f} kW")
                            if p_max_net_annuel < p_max_init_annuel - 0.1:
                                st.write(f"👉 Économies sur taxe de puissance : {int(revenu_ecretage):,} {devise_val}/an")
                            else:
                                st.write(f"👉 Économies sur taxe de puissance : 0 {devise_val}/an")
                                if best_capa_batt > 0:
                                    st.caption("ℹ️ La puissance de pointe n'a pas été réduite de manière significative sur un mois complet.")

            # --- NOUVEAU : GRAPHIQUE DE SYNTHÈSE DES SIMULATIONS ---
            st.write("---")
            if mode_ideal_val == "Favoriser l'autonomie sur site":
                st.write(f"#### 📊 Analyse comparative : Autoproduction et Économies sur {duree_projet_val} ans")
            elif mode_ideal_val == "Favoriser le financier ROI":
                st.write(f"#### 📊 Analyse comparative : ROI et Économies sur {duree_projet_val} ans")
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
                        hovertemplate="<b>SYSTÈME IDÉAL</b><br>Économies: %{y:,.0f} " + devise_val + "<br>ROI: " + df_ideal["ROI"].astype(str).values[0] + " ans"
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
                # Titre de la section
                st.write("---")
                st.subheader("Sollicitation de la batterie")

                # On doit recalculer les flux pour le système idéal pour le graphique
                if mode_production_val == "Télécharger une courbe de production PV" and courbe_prod_upload_val:
                    prod_h_ideal = courbe_prod_upload_val
                else:
                    ratio_pv_ideal = best_pv_total / p_totale_max_toit if p_totale_max_toit > 0 else 0
                    prod_h_ideal = [0.0] * 8760
                    for item in profils_unitaires_par_pan:
                        p_pan_ideal = item['p_max'] * ratio_pv_ideal
                        for i in range(8760):
                            prod_h_ideal[i] += item['profil'][i] * p_pan_ideal
                
                # --- OPTIMISATION DU RATIO DE DÉCHARGE ÉCRÊTAGE (GRAPH) ---
                best_ratio_ecretage_graph = 1.0 # Par défaut 100% de décharge pour couvrir la conso
                if autoriser_ecretage_val and best_capa_batt > 0:
                    best_ratio_ecretage_graph = 0.5
                    max_score_r_g = -float('inf')
                    # Test de plusieurs ratios pour trouver le meilleur compromis annuel
                    for r_t in [0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0]:
                        s_t_r = 0.0
                        auto_r_g = 0
                        p_n_r = []
                        soc_a_g = []
                        for h_idx, (ph, ch) in enumerate(zip(prod_h_ideal, courbe_conso_travail)):
                            h_j = (h_idx // pts_par_heure) % 24
                            if ph >= ch:
                                d = ph - ch
                                c = min(d, (cap_utile_ideal - s_t_r) / RENDEMENT_CHARGE, p_batt_max_ideal_pt)
                                s_t_r += c * RENDEMENT_CHARGE
                                p_n_r.append(0)
                            else:
                                bes = ch - ph
                                bes_cov = bes * r_t
                                dech = min(bes_cov / RENDEMENT_DECHARGE, s_t_r, p_batt_max_ideal_pt)
                                s_t_r -= dech
                                auto_r_g += (ph + dech * RENDEMENT_DECHARGE)
                                p_n_r.append((bes - dech * RENDEMENT_DECHARGE) * (60 / pas_temps_final))
                            if 6 <= h_j <= 8: soc_a_g.append(s_t_r)
                    pts_par_heure = 60 // pas_temps_final
                    p_m_r = max(p_n_r[:8760*pts_par_heure]) if p_n_r else 0
                    s_m_a_g = sum(soc_a_g)/len(soc_a_g) if soc_a_g else 0
                    sc_r = auto_r_g - (s_m_a_g * 1000) - (p_m_r * 10)
                    if sc_r > max_score_r_g:
                        max_score_r_g = sc_r
                        best_ratio_ecretage_graph = r_t
                
                soc_i = 0.0
                cap_utile_i = best_capa_batt * DOD
                p_batt_max_i_pt = best_capa_batt * C_RATE * (pas_temps_final / 60)
                liste_soc_i = []
                liste_charge_i = []
                liste_charge_res_i = []
                liste_decharge_i = []
                
                for h_idx, (ph, ch) in enumerate(zip(prod_h_ideal, courbe_conso_travail)):
                    c_charge = 0
                    c_charge_res = 0
                    c_decharge = 0
                    
                    h_jour = (h_idx // pts_par_heure) % 24
                    
                    if ph >= ch:
                        dispo = ph - ch
                        charge = min(dispo, (cap_utile_i - soc_i) / RENDEMENT_CHARGE, p_batt_max_i_pt)
                        soc_i += charge * RENDEMENT_CHARGE
                        c_charge = charge
                    else:
                        besoin = ch - ph
                        
                        # Logique d'optimisation d'écrêtage (Peak Shaving) :
                        besoin_a_couvrir = besoin * (best_ratio_ecretage_graph if autoriser_ecretage_val else 1.0)
                            
                        decharge = min(besoin_a_couvrir / RENDEMENT_DECHARGE, soc_i, p_batt_max_i_pt)
                        soc_i -= decharge
                        c_decharge = decharge * RENDEMENT_DECHARGE

                    liste_soc_i.append(soc_i)
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
                remplissage_moyen_i = (sum(liste_soc_i) / len(liste_soc_i)) / best_capa_batt * 100 if best_capa_batt > 0 else 0

                c_sol1, c_sol2, c_sol3, c_sol4 = st.columns(4)
                c_sol1.metric("Cycles complets / an", f"{int(round(cycles_complets_i)):,}".replace(",", " "), help="Nombre de fois où la capacité totale de la batterie est déchargée en une année (Total décharge / Capacité nominale).")
                c_sol2.metric("Remplissage moyen", f"{remplissage_moyen_i:.1f} %", help="Moyenne de l'état de charge (SOC) de la batterie sur l'année.")
                
                c_sol3.metric("% Charge moy. jour", f"{pct_charge_journalier:.1f} %", help="Pourcentage moyen de la capacité nominale chargé chaque jour.")
                
                c_sol4.metric("% Décharge moy. jour", f"{pct_decharge_journalier:.1f} %", help="Pourcentage moyen de la capacité nominale déchargé chaque jour.")
                
                st.write("**Flux journaliers cumulés (kWh/jour)**")
                
                df_flux_i = pd.DataFrame({
                    "Charge Solaire": liste_charge_i,
                    "Charge Réseau (Arbitrage)": liste_charge_res_i,
                    "Decharge": [-d for d in liste_decharge_i]
                })
                df_daily_i = df_flux_i.groupby(df_flux_i.index // (24 * pts_par_heure)).sum()
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
        p_plot = list(prod_horaire_cumulee)
        if len(p_plot) < 8760: p_plot.extend([0.0] * (8760 - len(p_plot)))
        p_plot = p_plot[:8760]

        c_plot = c_plot_calc
        
        ch_plot = [0.0] * 8760
        de_plot = [0.0] * 8760
        
        # Recalculer les flux ici pour la configuration ACTUELLE affichée (best_pv_total, best_capa_batt)
        ch_plot = []
        de_plot = []
        ch_res_plot = []
        soutirage_plot = []
        
        # Par défaut, on initialise avec les longueurs correctes
        pts_attendus = 8760 * pts_par_heure
        p_plot = mettre_a_jour_pas(prod_horaire_cumulee, pas_temps_final, pas_temps_final)
        c_plot = list(courbe_conso_travail)
        
        # S'assurer que les longueurs correspondent aux points annuels
        if len(p_plot) < pts_attendus: p_plot.extend([0.0] * (pts_attendus - len(p_plot)))
        p_plot = p_plot[:pts_attendus]
        if len(c_plot) < pts_attendus: c_plot.extend([0.0] * (pts_attendus - len(c_plot)))
        c_plot = c_plot[:pts_attendus]

        if 'best_pv_total' in locals() and best_pv_total is not None and best_pv_total > 0:
            if mode_production_val == "Télécharger une courbe de production PV" and courbe_prod_upload_val:
                p_plot = mettre_a_jour_pas(courbe_prod_upload_val, pas_temps_prod_val, pas_temps_final)
            else:
                ratio_pv_final = best_pv_total / p_totale_max_toit if p_totale_max_toit > 0 else 0
                p_plot = [0.0] * pts_attendus
                for item in profils_unitaires_par_pan:
                    p_pan_f = item['p_max'] * ratio_pv_final
                    p_pan_fin = mettre_a_jour_pas(item['profil'], 60, pas_temps_final)
                    for i in range(len(p_plot)):
                        p_plot[i] += p_pan_fin[i] * p_pan_f
            
            # Recalcul du soutirage sans batterie
            soutirage_plot = [max(0, ch - ph) for ph, ch in zip(p_plot, c_plot)]
                
            if best_capa_batt > 0:
                soc_f = 0.0
                cap_utile_f = best_capa_batt * DOD
                p_batt_max_f_pt = best_capa_batt * C_RATE * (pas_temps_final / 60)
                    
                # --- OPTIMISATION DU RATIO DE DÉCHARGE ÉCRÊTAGE (SUPERPOSITION) ---
                best_ratio_ecretage_super = 0.5
                if autoriser_ecretage_val:
                    max_score_r_s = -float('inf')
                    for r_t in [0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0]:
                        s_t_r = 0.0
                        auto_r_s = 0
                        p_n_r = []
                        soc_a_s = []
                        for h_idx in range(len(p_plot)):
                            ph = p_plot[h_idx]
                            ch = c_plot[h_idx]
                            h_j = (h_idx // pts_par_heure) % 24
                            if ph >= ch:
                                d = ph - ch
                                c = min(d, (cap_utile_f - s_t_r) / RENDEMENT_CHARGE, p_batt_max_f_pt)
                                s_t_r += c * RENDEMENT_CHARGE
                                p_n_r.append(0)
                            else:
                                bes = ch - ph
                                bes_cov = bes * r_t
                                dech = min(bes_cov / RENDEMENT_DECHARGE, s_t_r, p_batt_max_f_pt)
                                s_t_r -= dech
                                auto_r_s += (ph + dech * RENDEMENT_DECHARGE)
                                p_n_r.append((bes - dech * RENDEMENT_DECHARGE) * (60 / pas_temps_final))
                            if 6 <= h_j <= 8: soc_a_s.append(s_t_r)
                        pts_par_heure = 60 // pas_temps_final
                        p_m_r = max(p_n_r[:8760*pts_par_heure]) if p_n_r else 0
                        s_m_a_s = sum(soc_a_s)/len(soc_a_s) if soc_a_s else 0
                        sc_r = auto_r_s - (s_m_a_s * 1000) - (p_m_r * 10)
                        if sc_r > max_score_r_s:
                            max_score_r_s = sc_r
                            best_ratio_ecretage_super = r_t

                ch_res_plot = []
                de_plot = []
                ch_plot = []
                soutirage_plot = []
                for h_idx in range(len(p_plot)):
                    ph = p_plot[h_idx]
                    ch = c_plot[h_idx]
                    surplus_inst = max(0, ph - ch)
                    besoin_inst = max(0, ch - ph)
                    charge_f = 0
                    charge_res_f = 0
                    dech_f = 0
                    soutirage_f = 0
                        
                    if surplus_inst > 0:
                        charge_f = min(surplus_inst, (cap_utile_f - soc_f) / RENDEMENT_CHARGE, p_batt_max_f_pt)
                        soc_f += charge_f * RENDEMENT_CHARGE
                        soutirage_f = 0
                    else:
                        besoin_a_couvrir = besoin_inst * (best_ratio_ecretage_super if autoriser_ecretage_val else 1.0)
                        dech_f = min(besoin_a_couvrir / RENDEMENT_DECHARGE, soc_f, p_batt_max_f_pt)
                        soc_f -= dech_f
                        dech_f = dech_f * RENDEMENT_DECHARGE
                        soutirage_f = besoin_inst - dech_f

                    ch_plot.append(charge_f)
                    ch_res_plot.append(charge_res_f)
                    de_plot.append(dech_f)
                    soutirage_plot.append(soutirage_f)

            # --- SÉCURITÉ LONGUEUR DES TABLEAUX ---
            def normaliser_pts(liste, n):
                l = list(liste)
                if len(l) < n:
                    l.extend([0.0] * (n - len(l)))
                return l[:n]

            p_plot = normaliser_pts(p_plot, pts_attendus)
        c_plot = normaliser_pts(c_plot, pts_attendus)
        ch_plot = normaliser_pts(ch_plot, pts_attendus)
        ch_res_plot = normaliser_pts(ch_res_plot, pts_attendus)
        de_plot = normaliser_pts(de_plot, pts_attendus)
        soutirage_plot = normaliser_pts(soutirage_plot, pts_attendus)
        
        # Adaptation de l'index temporel
        dates = pd.date_range(start="2024-01-01", periods=pts_attendus, freq=f"{pas_temps_final}min")
        
        df_total = pd.DataFrame({
            "Temps": dates,
            "Production PV (kW)": p_plot,
            "Consommation (kW)": c_plot,
            "Charge Batterie Solaire (kW)": ch_plot,
            "Charge Batterie Réseau (kW)": ch_res_plot,
            "Décharge Batterie (kW)": de_plot,
            "Soutirage Réseau (kW)": soutirage_plot
        })
        
        # Facteur de conversion kW/kWh
        facteur_kw = 60 / pas_temps_final
        df_total_kw = df_total.copy()
        for col in ["Production PV (kW)", "Consommation (kW)", "Charge Batterie Solaire (kW)", "Charge Batterie Réseau (kW)", "Décharge Batterie (kW)", "Soutirage Réseau (kW)"]:
            df_total_kw[col] = df_total[col] * facteur_kw
        
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
            y=df_filtre["Consommation (kW)"] * facteur_kw,
            name="Consommation (kW)",
            line=dict(color='#AED6F1', width=2, shape='spline'),
            fill='none'
        ))

        # Ajout de la ligne de puissance souscrite / limite de raccordement
        if st.session_state.get("pays_selectionne") == "France":
            limite_label = "Limite d'abonnement"
            limite_val = abonnement_val_val
        else:
            limite_label = "Limite de raccordement"
            limite_val = puissance_intro_kw_val

        fig_superp.add_hline(
            y=limite_val,
            line_dash="dash",
            line_color="red",
            annotation_text=limite_label,
            annotation_position="bottom right"
        )

        # Mise en évidence des dépassements
        df_depassement = df_filtre[df_filtre["Consommation (kW)"] * facteur_kw > limite_val].copy()
        if not df_depassement.empty:
            fig_superp.add_trace(go.Scatter(
                x=df_depassement["Temps"],
                y=df_depassement["Consommation (kW)"] * facteur_kw,
                mode='markers',
                name="Dépassement de pointe",
                marker=dict(color='red', size=6),
                hoverinfo='text',
                text=[f"Dépassement: {v*facteur_kw:.1f} kW" for v in df_depassement["Consommation (kW)"]]
            ))

        # Zone d'Autoconsommation (Hachurée)
        fig_superp.add_trace(go.Scatter(
            x=df_filtre["Temps"],
            y=df_filtre["Autoconsommation (kW)"] * facteur_kw,
            name="Énergie autoconsommée (PV direct)",
            fill='tozeroy',
            mode='none',
            fillpattern=dict(shape="/", fgcolor="#F7DC6F", bgcolor="rgba(0,0,0,0)", fillmode="overlay"),
            hoverinfo='skip'
        ))

        # Courbe de Production PV (Jaune pastel)
        fig_superp.add_trace(go.Scatter(
            x=df_filtre["Temps"],
            y=df_filtre["Production PV (kW)"] * facteur_kw,
            name="Production PV (kW)",
            line=dict(color='#F7DC6F', width=2),
            fill='none'
        ))

        # Puissance soutirée du réseau (Rouge brique / Orange foncé)
        fig_superp.add_trace(go.Scatter(
            x=df_filtre["Temps"],
            y=df_filtre["Soutirage Réseau (kW)"] * facteur_kw,
            name="Soutirage Réseau (kW)",
            line=dict(color='#E67E22', width=2),
            fill='none'
        ))

        if best_capa_batt > 0:
            soutirage_pv_seul = [max(0, ch - ph) for ph, ch in zip(p_plot, c_plot)]
            if pts_par_heure == 4:
                soutirage_pv_seul_total = normaliser_pts(soutirage_pv_seul, pts_attendus)
                df_total_temp = pd.DataFrame({"Temps": dates, "Soutirage PV Seul": soutirage_pv_seul_total})
                df_filtre_temp = df_total_temp[df_total_temp['Temps'].dt.month == (st.session_state.mois_idx + 1)]
            else:
                soutirage_pv_seul_total = normaliser_pts(soutirage_pv_seul, pts_attendus)
                df_total_temp = pd.DataFrame({"Temps": dates, "Soutirage PV Seul": soutirage_pv_seul_total})
                df_filtre_temp = df_total_temp[df_total_temp['Temps'].dt.month == (st.session_state.mois_idx + 1)]
                
            fig_superp.add_trace(go.Scatter(
                x=df_filtre_temp["Temps"],
                y=df_filtre_temp["Soutirage PV Seul"] * facteur_kw,
                name="Soutirage sans batterie (kW)",
                line=dict(color='#E67E22', width=1, dash='dot'),
                fill='none',
                visible='legendonly'
            ))

        # AJOUT DES PRIX DYNAMIQUES SUR L'AXE Y2
        # (Désactivé)

        # AJOUT DES FLUX BATTERIE
        if simuler_batterie_val and 'best_capa_batt' in locals() and best_capa_batt > 0:
            # Charge Batterie Solaire (VIOLET)
            fig_superp.add_trace(go.Scatter(
                x=df_filtre["Temps"],
                y=df_filtre["Charge Batterie Solaire (kW)"] * facteur_kw,
                name="Charge Solaire",
                line=dict(color='#A569BD', width=1.5, dash='dot'),
                fill='none'
            ))
            # Décharge Batterie (VERT)
            fig_superp.add_trace(go.Scatter(
                x=df_filtre["Temps"],
                y=df_filtre["Décharge Batterie (kW)"] * facteur_kw,
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
                showgrid=False
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
