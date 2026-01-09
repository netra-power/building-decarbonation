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
    surface_dispo = st.sidebar.number_input("Surface totale (m²)", min_value=1, value=50, step=1)
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
        surf = st.sidebar.number_input("Surface disponible (m²)", min_value=1, value=50)
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
        min_value=1, 
        value=25, 
        step=1, 
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

        import math

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
        if puissance_pv_installable > 0:
            facteur_limite = puissance_retenue / puissance_pv_installable
            production_totale_an *= facteur_limite
            prod_mensuelle_cumulee = [p * facteur_limite for p in prod_mensuelle_cumulee]
            prod_horaire_cumulee = [p * facteur_limite for p in prod_horaire_cumulee]
        
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
            st.metric("Productible (PVGIS)", f"{productible_moyen:,.0f} kWh/kWc/an")
            
            # Affichage de la puissance et du nombre de modules sur la même ligne via Markdown
            st.write("**Puissance installable**")
            st.markdown(f"""
                <div style="display: flex; align-items: baseline; gap: 10px;">
                    <span style="font-size: 2rem; font-weight: 600;">{puissance_retenue:.1f} kWc</span>
                    <span style="font-size: 1rem; color: #666;">(soit {nb_modules_total} modules de 500 Wc)</span>
                </div>
                """, unsafe_allow_html=True)
            
            st.markdown(f"""
                <div style="font-size: 0.8rem; color: #555; background-color: #e7f3fe; padding: 10px; border-radius: 5px; border-left: 5px solid #2196F3; margin-top: 10px; margin-bottom: 15px;">
                    💡 La puissance installable est le minimum entre la capacité de votre toit et la puissance de votre raccordement électrique.
                </div>
                """, unsafe_allow_html=True)
            st.metric("Production annuelle totale", f"{production_totale_an:,.0f} kWh/an")
            
            # Affichage du graphique de production mensuelle dans la colonne de gauche uniquement
            st.write("---")
            st.subheader("📊 Répartition mensuelle")
            mois_noms = ["Jan", "Fév", "Mar", "Avr", "Mai", "Juin", "Juil", "Août", "Sep", "Oct", "Nov", "Déc"]
            
            df_mensuel = pd.DataFrame({
                "Mois": mois_noms,
                "Production (kWh)": prod_mensuelle_cumulee
            })
            df_mensuel["Mois"] = pd.Categorical(df_mensuel["Mois"], categories=mois_noms, ordered=True)
            
            import plotly.express as px
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
            st.write(f"**Potentiel toiture :** {puissance_pv_installable:.1f} kWc")
            st.write("**Détail par orientation :**")
            for d in details_pans_calcul:
                st.write(f"- {d['orientation']} : {d['puissance']:.1f} kWc ({d['nb_mods']} modules)")

        # --- SECTION AUTOCONSOMMATION ---
        st.write("---")
        st.header("⚡ Analyse de l'autoconsommation")
        
        # --- ÉTAPE 5 : DIMENSIONNEMENT DE LA BATTERIE ---
        st.subheader("🔋 Dimensionnement du stockage (Batterie)")
        activer_batterie = st.toggle("Simuler un système de stockage", value=False)
        
        capa_batterie = 0.0
        autoconsommation_finale_kwh = autoconsommation_kwh
        surplus_final_kwh = surplus_injecte_kwh
        
        if activer_batterie:
            # Suggestion automatique : environ 1 à 1.5 kWh par kWc installé
            suggestion_batterie = round(puissance_retenue * 1.2, 1)
            capa_batterie = st.number_input("Capacité de la batterie (kWh)", min_value=0.0, value=float(suggestion_batterie), step=0.5)
            
            # Simulation batterie dynamique
            soc = 0.0 # State of Charge
            autoconsommation_finale_kwh = 0
            surplus_final_kwh = 0
            
            for p, c in zip(prod_horaire_cumulee, courbe_conso):
                if p >= c:
                    # Surplus de production
                    autoconsommation_finale_kwh += c
                    dispo_pour_batterie = p - c
                    # On charge la batterie
                    charge = min(dispo_pour_batterie, capa_batterie - soc)
                    soc += charge
                    surplus_final_kwh += (dispo_pour_batterie - charge)
                else:
                    # Déficit de production
                    autoconsommation_finale_kwh += p
                    besoin = c - p
                    # On décharge la batterie
                    decharge = min(besoin, soc)
                    soc -= decharge
                    autoconsommation_finale_kwh += decharge
            
            taux_autoconsommation_final = (autoconsommation_finale_kwh / production_totale_an * 100) if production_totale_an > 0 else 0
            taux_autoproduction_final = (autoconsommation_finale_kwh / conso_annuelle_kwh * 100) if conso_annuelle_kwh > 0 else 0
            
            st.write(f"Avec une batterie de **{capa_batterie} kWh** :")
            c1, c2, c3 = st.columns(3)
            c1.metric("Nouveau Taux d'autoconsommation", f"{taux_autoconsommation_final:.1f} %", delta=f"{taux_autoconsommation_final - taux_autoconsommation:.1f} %")
            c2.metric("Nouveau Taux d'autoproduction", f"{taux_autoproduction_final:.1f} %", delta=f"{taux_autoproduction_final - taux_autoproduction:.1f} %")
            c3.metric("Nouveau Surplus", f"{surplus_final_kwh:,.0f} kWh")
        else:
            col_res1, col_res2, col_res3 = st.columns(3)
            col_res1.metric("Taux d'autoconsommation", f"{taux_autoconsommation:.1f} %", help="Part de la production PV consommée sur place.")
            col_res2.metric("Taux d'autoproduction", f"{taux_autoproduction:.1f} %", help="Part de la consommation totale couverte par le PV.")
            col_res3.metric("Surplus rejeté", f"{surplus_injecte_kwh:,.0f} kWh", help="Énergie réinjectée sur le réseau.")
            taux_autoconsommation_final = taux_autoconsommation
            taux_autoproduction_final = taux_autoproduction

        # Graphique de superposition avec sélection par mois
        st.write("---")
        st.subheader("📊 Superposition Production vs Consommation")
        
        dates = pd.date_range(start="2024-01-01", periods=8760, freq="H")
        df_total = pd.DataFrame({
            "Temps": dates,
            "Production PV (kW)": prod_horaire_cumulee,
            "Consommation (kW)": courbe_conso
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

        import plotly.graph_objects as go

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
        
        
# Affichage du graphique de production mensuelle PVGIS - RETIRÉ DU BAS
        # st.write("---")
        # ...
else:
    st.info("En attente d'une adresse valide pour calculer le productible via PVGIS.")
