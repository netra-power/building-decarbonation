import streamlit as st
import pandas as pd
import numpy as np
import requests
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
    st.session_state.adresse_validee = None

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

if not st.session_state.adresse_validee:
    st.sidebar.info("👋 Bienvenue ! Veuillez sélectionner votre pays puis saisir votre adresse pour lancer l'étude.")
    st.sidebar.write("🌍 **Étape 1 : Choisir le pays**")
    pays_selectionne = st.sidebar.selectbox("Sélectionnez votre pays", ["France", "Suisse"], label_visibility="collapsed")
    
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

adresse = st.session_state.adresse_validee

# 2. Type de toit et Matériaux
st.sidebar.write("🏠 **Étape 3 : Caractéristiques du bâtiment**")
type_toit = st.sidebar.selectbox("Votre type de toit", ["Plat", "Incliné"])

if type_toit == "Plat":
    materiau = st.sidebar.selectbox("Matériau", ["Bitumineux", "Gravier"])
    inclinaison_val = 10 # Valeur fixe par défaut pour le calcul mais grisée visuellement
    st.sidebar.text_input("Inclinaison du toit (degrés)", value="10° (Toit plat)", disabled=True)
    inclinaison = 10
else:
    materiau = st.sidebar.selectbox("Matériau", ["Tuile", "Tôle", "Eternit"])
    inclinaison = st.sidebar.slider("Inclinaison du toit (degrés)", 0, 90, 10)

# 3. Orientation
st.sidebar.subheader("Orientation")
mode_orientation = st.sidebar.radio("Type d'orientation", ["Mono-orientation", "Multi-orientations"])

orientations_possibles = [
    "Nord", "Nord-Est", "Est", "Sud-Est", 
    "Sud", "Sud-Ouest", "Ouest", "Nord-Ouest"
]
selection_orientations = []

if mode_orientation == "Mono-orientation":
    choix = st.sidebar.selectbox("Choisir l'orientation", orientations_possibles, index=4) # Sud par défaut
    selection_orientations.append(choix)
else:
    selection_orientations = st.sidebar.multiselect("Choisir les orientations", orientations_possibles, default=["Sud-Est", "Sud-Ouest"])

# --- LOGIQUE DE CALCUL (PVGIS) ---
if adresse:
    lat, lon = obtenir_lat_lon(adresse)

    if lat and lon:
        productibles = []
        details_pvgis = []
        
        for o in selection_orientations:
            aspect = get_aspect(o)
            prod = appeler_pvgis(lat, lon, inclinaison, aspect)
            if prod:
                productibles.append(prod)
                details_pvgis.append((o, prod))
        
        productible_moyen = sum(productibles) / len(productibles) if productibles else 0
    else:
        st.warning("⚠️ Géolocalisation impossible. Veuillez vérifier l'adresse.")
        productible_moyen = 0
        details_pvgis = []
else:
    productible_moyen = 0
    details_pvgis = []
    lat, lon = None, None

# --- AFFICHAGE DES RÉSULTATS ---
st.header("Analyse du productible photovoltaïque")

if not adresse:
    st.write("En attente de la saisie d'une adresse dans la barre latérale...")
elif lat and lon:
    col1, col2 = st.columns(2)

    with col1:
        st.metric("Productible estimé (PVGIS)", f"{productible_moyen:,.0f} kWh/kWc/an")
        st.write(f"**Adresse détectée :** {adresse}")
        st.write(f"**Coordonnées :** {lat:.4f}, {lon:.4f}")
        st.write(f"**Type de toit :** {type_toit} ({materiau})")

    with col2:
        st.write("**Détail par orientation :**")
        for o, p in details_pvgis:
            st.write(f"- {o} : {p:,.0f} kWh/kWc/an")
    
    st.info("💡 Ce productible est calculé via l'API PVGIS 5.2 en utilisant les données d'ensoleillement réelles de votre localisation.")
else:
    st.info("En attente d'une adresse valide pour calculer le productible via PVGIS.")
