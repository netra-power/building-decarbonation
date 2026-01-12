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
    # Adresse par défaut pour le développement
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
    surface_dispo = st.sidebar.number_input("Surface totale (m²)", min_value=1, value=150, step=1)
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
            incli = st.number_input("Inclinaison (°)", min_value=0, max_value=90, value=30)
        
        # Méthode de mesure juste avant surface disponible
        mode_mesure = st.sidebar.radio(
            "Méthode de mesure des surfaces", 
            ["Vue aérienne", "Surface réelle"], 
            horizontal=True,
            help="**Vue aérienne** : La surface est calculée comme une projection horizontale. L'outil appliquera un correctif trigonométrique selon l'inclinaison pour obtenir la surface réelle du toit."
        )
        surf = st.sidebar.number_input("Surface disponible (m²)", min_value=1, value=150)
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
        
        selection_multi = st.sidebar.multiselect("Sélectionnez les orientations", orientations_possibles, default=["Sud-Est", "Sud-Ouest"])
        
        # Tableau compact pour Multi-orientations avec titres
        if selection_multi:
            # On utilise des colonnes un peu plus larges pour les titres complets
            h1, h2, h3 = st.sidebar.columns([1.5, 1.8, 1.7])
            h1.caption("**Orientation**")
            h2.caption("**Inclinaison (°)**")
            h3.caption("**Surface (m²)**")
            
            for o in selection_multi:
                # Utilisation de colonnes alignées sans espacement vertical excessif
                c1, c2, c3 = st.sidebar.columns([1.5, 1.8, 1.7])
                with c1:
                    st.write(f"{o}")
                with c2:
                    incli = st.number_input(f"Incl. {o}", min_value=0, max_value=90, value=30, key=f"incli_{o}", label_visibility="collapsed")
                with c3:
                    surf = st.number_input(f"Surf. {o}", min_value=1, value=25, key=f"surf_{o}", label_visibility="collapsed")
                    st.markdown(f'<div style="font-size: 0.7rem; color: #666; margin-top: -10px;">👉 {surf:,.0f} m²</div>'.replace(",", " "), unsafe_allow_html=True)
                donnees_pans.append({"orientation": o, "inclinaison": incli, "surface": surf})

# --- ÉTAPE 4 : INTRODUCTION ÉLECTRIQUE ---
st.sidebar.write("🔌 **Introduction électrique**")
col_unit, col_val = st.sidebar.columns([1, 1])
with col_unit:
    unite_intro = st.selectbox(
        "Unité", 
        ["Ampères", "kVA"], 
        label_visibility="collapsed",
        help="L'unité de puissance d'introduction de votre bâtiment."
    )
with col_val:
    intro_val = st.number_input(
        f"Valeur Intro", 
        min_value=0.1, 
        value=9.9, 
        step=0.1, 
        label_visibility="collapsed",
        help="La valeur des kVA est normalement notée dans votre contrat d'abonnement ou sur vos factures d'électricité."
    )

# --- ÉTAPE 5 : CONSOMMATION ÉNERGÉTIQUE ---
st.sidebar.write("📈 **Étape 4 : Consommation énergétique**")

profil_conso = st.sidebar.selectbox(
    "Type de bâtiment",
    ["Résidentiel", "Tertiaire / Bureaux", "Industriel"]
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
            surf_hab = st.number_input("Surface ($m^2$)", min_value=1, value=100, step=10)
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
        surf_tert = st.sidebar.number_input("Surface totale ($m^2$)", min_value=1, value=500, step=100)
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
col_p1, col_p2 = st.sidebar.columns(2)
with col_p1:
    prix_achat = st.number_input("Prix achat (cts/kWh)", min_value=0.0, value=25.0, step=0.5)
with col_p2:
    prix_vente = st.number_input("Prix vente (cts/kWh)", min_value=0.0, value=12.0, step=0.5)

expander_f = st.sidebar.expander("Ratios d'investissement (CAPEX)")
with expander_f:
    capex_pv_kwc = st.number_input("CAPEX PV (€/kWc)", min_value=0, value=1500, step=50)
    capex_batt_kwh = st.number_input("CAPEX Batterie (€/kWh)", min_value=0, value=800, step=50)

else:
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
            st.subheader("☀️ Potentiel Solaire")
            st.metric("Productible (PVGIS)", f"{productible_moyen:,.0f} kWh/kWc/an".replace(",", " "))
            
            # Affichage de la puissance et du nombre de modules sur la même ligne via Markdown
            st.write("**Puissance installable**")
            st.markdown(f"""
                <div style="display: flex; align-items: baseline; gap: 10px;">
                    <span style="font-size: 2rem; font-weight: 600;">{puissance_retenue:,.1f} kWc</span>
                    <span style="font-size: 1rem; color: #666;">(soit {nb_modules_final:,.0f} modules de 500 Wc)</span>
                </div>
                """.replace(",", " "), unsafe_allow_html=True)
            
            st.markdown(f"""
                <div style="font-size: 0.8rem; color: #555; background-color: #e7f3fe; padding: 10px; border-radius: 5px; border-left: 5px solid #2196F3; margin-top: 10px; margin-bottom: 15px;">
                    💡 La puissance installable est le minimum entre la capacité de votre toit et la puissance de votre raccordement électrique.
                </div>
                """, unsafe_allow_html=True)
            st.metric("Production annuelle totale", f"{production_totale_an:,.0f} kWh/an".replace(",", " "))
            
            # Affichage du graphique de production mensuelle dans la colonne de gauche uniquement
            st.write("---")
            st.subheader("📊 Répartition mensuelle")
            mois_noms = ["Jan", "Fév", "Mar", "Avr", "Mai", "Juin", "Juil", "Août", "Sep", "Oct", "Nov", "Déc"]
            
            df_mensuel = pd.DataFrame({
                "Mois": mois_noms,
                "Production (kWh)": prod_mensuelle_cumulee
            })
            df_mensuel["Mois"] = pd.Categorical(df_mensuel["Mois"], categories=mois_noms, ordered=True)
            
            fig = px.bar(
                df_mensuel, 
                x="Mois", 
                y="Production (kWh)",
                color_discrete_sequence=["#F7DC6F"] # JAUNE PASTEL
            )
            fig.update_layout(
                margin=dict(l=0, r=0, t=0, b=0),
                height=300,
                xaxis_title=None,
                yaxis_title="kWh"
            )
            st.plotly_chart(fig, use_container_width=True, config={'displayModeBar': False})

        with col2:
            st.subheader("📍 Bâtiment")
            st.write(f"**Adresse :** {adresse}")
            
            # Affichage Introduction
            if unite_intro == "kVA":
                st.write(f"**Introduction :** {intro_val} kVA")
            else:
                equiv_kva = (400 * intro_val * 1.732) / 1000
                st.write(f"**Introduction :** {intro_val} Ampères (env. {equiv_kva:.1f} kVA)")

            # Affichage Consommation
            if conso_annuelle_kwh > 100000:
                st.write(f"**Consommation annuelle :** {int(round(conso_annuelle_kwh/1000))} MWh/an")
            else:
                st.write(f"**Consommation annuelle :** {conso_annuelle_kwh:,.0f} kWh/an")

            st.write(f"**Toiture :** {type_toit} ({materiau})")
            if type_toit == "Plat":
                pass
            st.write(f"**Potentiel toiture :** {puissance_pv_installable:,.1f} kWc".replace(",", " "))
            st.write("**Détail par orientation :**")
            for d in details_pans_calcul:
                st.write(f"- {d['orientation']} : {d['puissance']:,.1f} kWc ({d['nb_mods']:,} modules)".replace(",", " "))

        # --- SECTION AUTOCONSOMMATION ---
        st.write("---")
        st.header("⚡ Analyse de l'autoconsommation")
        
        st.write("**Bâtiment avec installation photovoltaïque seule**")
        col_res1, col_res2, col_res3 = st.columns(3)
        col_res1.metric("Autoconsommation", f"{taux_autoconsommation:.1f} %", help="Part de la production PV consommée sur place.")
        col_res2.metric("Autoproduction", f"{taux_autoproduction:.1f} %", help="Part de la consommation totale couverte par le PV.")
        col_res3.metric("Surplus rejeté", f"{surplus_injecte_kwh:,.0f} kWh".replace(",", " "), help="Énergie réinjectée sur le réseau.")
        
        activer_batterie = st.toggle("Simuler un système de stockage", value=False)
        
        capa_batterie = 0.0
        autoconsommation_finale_kwh = autoconsommation_kwh
        surplus_final_kwh = surplus_injecte_kwh
        liste_soc = [0.0] * 8760
        liste_charge = [0.0] * 8760
        liste_decharge = [0.0] * 8760
        
        # Paramètres batteries par défaut
        DOD = 0.90  # Profondeur de décharge (90%)
        RENDEMENT_CHARGE = 0.95
        RENDEMENT_DECHARGE = 0.95
        C_RATE = 0.5  # Puissance max = 0.5 * Capacité (Système 2h)

        if activer_batterie:
            st.write("**Bâtiment avec installation photovoltaïque et batterie**")
            
            # --- CALCUL DE L'OPTIMUM DE BATTERIE PAR INTERPOLATION ---
            with st.spinner("Optimisation de la batterie selon vos objectifs (100% Autonomie)..."):
                # On teste une plage plus large pour viser l'autonomie totale
                pas = puissance_retenue * 0.3
                test_caps = [i * pas for i in range(1, 21)] # On teste 20 points jusqu'à 6x la puissance PV
                resultats_test = []
                
                for cap in test_caps:
                    cap_utile = cap * DOD
                    temp_soc = 0.0
                    temp_auto_kwh = 0
                    p_batt_max = cap * C_RATE
                    for p, c in zip(prod_horaire_cumulee, courbe_conso):
                        if p >= c:
                            temp_auto_kwh += c
                            surplus = p - c
                            # On charge la batterie : limitée par surplus, place utile ET puissance batterie
                            # On considère que p_batt_max est la puissance côté AC/onduleur
                            charge_possible = min(surplus, (cap_utile - temp_soc) / RENDEMENT_CHARGE, p_batt_max)
                            temp_soc += charge_possible * RENDEMENT_CHARGE
                        else:
                            temp_auto_kwh += p
                            besoin = c - p
                            # On décharge la batterie : limitée par besoin, stock utile ET puissance batterie
                            decharge_possible = min(besoin / RENDEMENT_DECHARGE, temp_soc, p_batt_max)
                            temp_soc -= decharge_possible
                            temp_auto_kwh += decharge_possible * RENDEMENT_DECHARGE
                    resultats_test.append(temp_auto_kwh)
                
                # Identification de l'optimum selon la hiérarchie d'objectifs
                optimum_val = 0.0
                objectif_atteint = ""
                
                for i in range(len(resultats_test)):
                    cap = test_caps[i]
                    gain = resultats_test[i]
                    
                    t_prod = (gain / conso_annuelle_kwh * 100) if conso_annuelle_kwh > 0 else 0
                    t_cons = (gain / production_totale_an * 100) if production_totale_an > 0 else 0
                    
                    # 1. Optimum de rendement marginal (On s'arrête si ajouter de la batterie rapporte moins de 0.5% d'autoproduction)
                    if i > 0:
                        gain_marginal = (resultats_test[i] - resultats_test[i-1]) / conso_annuelle_kwh * 100 if conso_annuelle_kwh > 0 else 0
                        if gain_marginal < 0.5:
                            optimum_val = test_caps[i-1]
                            objectif_atteint = "⚖️ Optimum de rendement identifié : Au-delà de cette capacité, l'ajout de batterie devient peu rentable."
                            break

                    # 2. Priorité : 100% Autoproduction (Autonomie)
                    if t_prod >= 99.0:
                        optimum_val = cap
                        objectif_atteint = "🌟 Objectif 100% Autoproduction atteint : Votre bâtiment est désormais totalement autonome."
                        break
                    
                    # 3. Seconde priorité : 100% Autoconsommation (Zéro Rejet)
                    if t_cons >= 99.0:
                        optimum_val = cap
                        objectif_atteint = "✅ Objectif 100% Autoconsommation atteint : Vous consommez l'intégralité de votre production solaire."
                        break
                    
                    optimum_val = cap
                    objectif_atteint = "⚠️ Limite technique atteinte : La production solaire est insuffisante pour charger une plus grosse batterie."

                suggestion_batterie = round(optimum_val, 1)

            c_capa, c_info = st.columns([1, 2])
            with c_capa:
                capa_batterie = st.number_input("Capacité optimale (kWh) 🔋", min_value=0.0, value=float(int(suggestion_batterie)), step=1.0)
            
            with c_info:
                st.info(f"{objectif_atteint}\n\nCapacité recommandée : **{int(suggestion_batterie):,} kWh**".replace(",", " "))
            
            # Simulation batterie finale avec la valeur choisie
            soc = 0.0 # State of Charge
            cap_utile_finale = capa_batterie * DOD
            autoconsommation_finale_kwh = 0
            surplus_final_kwh = 0
            liste_soc = []
            liste_charge = []
            liste_decharge = []
            
            # Puissance de batterie (Système 2h par défaut : 0.5C)
            p_batt_max = capa_batterie * C_RATE
            
            for p, c in zip(prod_horaire_cumulee, courbe_conso):
                current_charge = 0
                current_decharge = 0
                if p >= c:
                    # Surplus de production
                    autoconsommation_finale_kwh += c
                    dispo_pour_batterie = p - c
                    # Charge : limitée par surplus, place utile ET puissance batterie
                    charge = min(dispo_pour_batterie, (cap_utile_finale - soc) / RENDEMENT_CHARGE, p_batt_max)
                    soc += charge * RENDEMENT_CHARGE
                    current_charge = charge
                    surplus_final_kwh += (dispo_pour_batterie - charge)
                else:
                    # Déficit de production
                    autoconsommation_finale_kwh += p
                    besoin = c - p
                    # Décharge : limitée par besoin, stock utile ET puissance batterie
                    decharge = min(besoin / RENDEMENT_DECHARGE, soc, p_batt_max)
                    soc -= decharge
                    current_decharge = decharge * RENDEMENT_DECHARGE
                    autoconsommation_finale_kwh += current_decharge
                liste_soc.append(soc)
                liste_charge.append(current_charge)
                liste_decharge.append(current_decharge)
            
            taux_autoconsommation_final = (autoconsommation_finale_kwh / production_totale_an * 100) if production_totale_an > 0 else 0
            taux_autoproduction_final = (autoconsommation_finale_kwh / conso_annuelle_kwh * 100) if conso_annuelle_kwh > 0 else 0
            
            # Affichage des métriques batterie
            col_batt1, col_batt2, col_batt3 = st.columns(3)
            col_batt1.metric("Autoconsommation", f"{taux_autoconsommation_final:.1f} %", delta=f"{taux_autoconsommation_final - taux_autoconsommation:.1f} %")
            col_batt2.metric("Autoproduction", f"{taux_autoproduction_final:.1f} %", delta=f"{taux_autoproduction_final - taux_autoproduction:.1f} %")
            col_batt3.metric("Surplus", f"{surplus_final_kwh:,.0f} kWh".replace(",", " "), delta=f"{surplus_final_kwh - surplus_injecte_kwh:,.0f} kWh".replace(",", " "), delta_color="inverse")

        else:
            taux_autoconsommation_final = taux_autoconsommation
            taux_autoproduction_final = taux_autoproduction

        # --- SECTION DIMENSIONNEMENT IDÉAL (AUTONOMIE TOTALE) ---
        st.write("---")
        st.header("🌟 Dimensionnement idéal pour l'autonomie totale")
        st.write("Ce système est calculé pour viser 100% d'autonomie en optimisant la taille du système (PV + Batterie) selon vos besoins réels, sans dépasser le potentiel de votre toiture.")

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

        # 2. Recherche du dimensionnement PV + Batterie optimal
        # On va tester différentes tailles de PV (de 10% à 100% du max)
        # Et pour chaque taille, chercher la batterie idéale.
        
        if profils_unitaires_par_pan and conso_annuelle_kwh > 0:
            best_pv_total = 0
            best_capa_batt = 0
            best_autoprod = 0
            objectif_trouve = False
            
            # On définit des paliers de test pour la puissance PV (max 20 paliers)
            # On limite la recherche à une valeur raisonnable par rapport à la consommation (max 4x la puissance de couverture)
            # pour garder une résolution fine même sur des surfaces démesurées.
            p_totale_max_toit = sum(p['p_max'] for p in profils_unitaires_par_pan)
            p_couverture_conso = conso_annuelle_kwh / productible_moyen if productible_moyen > 0 else 20.0
            p_recherche_max = min(p_totale_max_toit, max(p_couverture_conso * 4, 10.0))
            
            pas_pv = max(0.5, p_recherche_max / 20)
            paliers_pv = [i * pas_pv for i in range(1, 21)]
            
            # On ajoute le max de la toiture en dernier recours
            paliers_pv.append(p_totale_max_toit)
            
            # On s'assure que la liste est triée et sans doublons
            paliers_pv = sorted(list(set(paliers_pv)))
            
            p_totale_max = p_totale_max_toit # Pour la distribution au prorata
            
            for p_test in paliers_pv:
                if objectif_trouve: break
                
                # Distribution de la puissance p_test sur les pans au prorata de leur p_max
                ratio_pv = p_test / p_totale_max
                prod_h_test = [0.0] * 8760
                for item in profils_unitaires_par_pan:
                    p_pan_test = item['p_max'] * ratio_pv
                    for i in range(8760):
                        prod_h_test[i] += item['profil'][i] * p_pan_test
                
                # Pour ce PV donné, on cherche la batterie optimale (jusqu'à 3kWh/kWc)
                cap_max_batt = p_test * 3.0
                pas_b = max(1.0, cap_max_batt / 15)
                
                last_t_auto_for_this_pv = 0.0
                for cap_b in [i * pas_b for i in range(16)]:
                    cap_utile_b = cap_b * DOD
                    s_temp = 0.0
                    a_temp = 0
                    p_batt_max_test = cap_b * C_RATE
                    
                    for ph, ch in zip(prod_h_test, courbe_conso):
                        if ph >= ch:
                            # Surplus
                            a_temp += ch
                            dispo = ph - ch
                            charge = min(dispo, (cap_utile_b - s_temp) / RENDEMENT_CHARGE, p_batt_max_test)
                            s_temp += charge * RENDEMENT_CHARGE
                        else:
                            # Déficit
                            a_temp += ph
                            besoin = ch - ph
                            decharge = min(besoin / RENDEMENT_DECHARGE, s_temp, p_batt_max_test)
                            s_temp -= decharge
                            a_temp += decharge * RENDEMENT_DECHARGE
                    
                    t_auto = (a_temp / conso_annuelle_kwh * 100)
                    
                    # Gain marginal de la batterie pour ce PV spécifique
                    gain_marginal_batt = t_auto - last_t_auto_for_this_pv
                    
                    # On cherche l'amélioration par rapport au meilleur système trouvé jusque là
                    # Pour éviter le surdimensionnement PV, on n'augmente le PV que si le gain est significatif (> 0.5%)
                    gain_vs_best = t_auto - best_autoprod
                    
                    if gain_vs_best > 0.5:
                        best_autoprod = t_auto
                        best_pv_total = p_test
                        best_capa_batt = cap_b
                    
                    if t_auto >= 99.0:
                        best_autoprod = t_auto
                        best_pv_total = p_test
                        best_capa_batt = cap_b
                        objectif_trouve = True
                        break
                    
                    # Si ajouter de la batterie pour ce PV n'apporte plus rien (< 0.5%), on passe au PV suivant
                    if cap_b > 0 and gain_marginal_batt < 0.5:
                        break
                    
                    last_t_auto_for_this_pv = t_auto
            
            aug_intro_ideale = max(0.0, best_pv_total - puissance_intro_kw)
            
            ci1, ci2, ci3 = st.columns(3)
            ci1.metric("Puissance PV Idéale", f"{best_pv_total:,.1f} kWc".replace(",", " "), help="Puissance PV minimale nécessaire pour atteindre l'optimum technique.")
            ci2.metric("Stockage Idéal", f"{int(best_capa_batt):,} kWh".replace(",", " "), help="Capacité de batterie couplée à la puissance PV idéale.")
            
            if aug_intro_ideale > 0:
                if unite_intro == "kVA":
                    label_aug = f"+{aug_intro_ideale:,.1f} kVA".replace(",", " ")
                else:
                    amp_aug = (aug_intro_ideale * 1000) / (400 * 1.732)
                    label_aug = f"+{amp_aug:,.1f} A".replace(",", " ")
                ci3.metric("Augmentation d'intro", label_aug, delta=f"Besoin de {best_pv_total:,.1f} kW au total".replace(",", " "), delta_color="inverse")
            else:
                ci3.metric("Augmentation d'intro", "Aucune", help="Votre introduction actuelle est suffisante pour ce système idéal.")
            
            if best_autoprod < 99.0:
                st.warning(f"⚠️ Même avec le plein potentiel de votre toiture, l'autonomie totale (100%) est difficile à atteindre. Maximum possible : {best_autoprod:.1f}% d'autoproduction.")
            else:
                st.success(f"✅ Objectif 100% Autoproduction atteint avec {best_pv_total:.1f} kWc et {int(best_capa_batt):,} kWh.".replace(",", " "))

            # --- ANALYSE DE LA SOLLICITATION DE LA BATTERIE IDÉALE ---
            if best_capa_batt > 0:
                # On doit recalculer les flux pour le système idéal pour le graphique
                ratio_pv_ideal = best_pv_total / p_totale_max if p_totale_max > 0 else 0
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
                st.subheader("Sollicitation de la batterie (Système idéal)")
                
                total_decharge_i = sum(liste_decharge_i)
                cycles_complets_i = total_decharge_i / best_capa_batt if best_capa_batt > 0 else 0
                remplissage_moyen_i = (sum(liste_soc_i) / len(liste_soc_i)) / cap_utile_i * 100 if cap_utile_i > 0 else 0
                
                c_sol1, c_sol2 = st.columns(2)
                c_sol1.metric("Cycles complets / an", f"{int(round(cycles_complets_i))}")
                c_sol2.metric("Remplissage moy / jour", f"{remplissage_moyen_i:.1f} %")
                
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
        
        dates = pd.date_range(start="2024-01-01", periods=8760, freq="H")
        df_total = pd.DataFrame({
            "Temps": dates,
            "Production PV (kW)": prod_horaire_cumulee,
            "Consommation (kW)": courbe_conso,
            "Charge Batterie (kW)": liste_charge,
            "Décharge Batterie (kW)": liste_decharge,
            "État de Charge (kWh)": liste_soc
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
            name="Énergie autoconsommée",
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

        # Tracés Batterie (si activée)
        if activer_batterie:
            # Charge Batterie (Violet)
            fig_superp.add_trace(go.Scatter(
                x=df_filtre["Temps"],
                y=df_filtre["Charge Batterie (kW)"],
                name="Charge Batterie (Stockage)",
                line=dict(color='#A569BD', width=1.5, dash='dot'),
                fill='none'
            ))
            # Décharge Batterie (Vert)
            fig_superp.add_trace(go.Scatter(
                x=df_filtre["Temps"],
                y=df_filtre["Décharge Batterie (kW)"],
                name="Décharge Batterie (Restitution)",
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
