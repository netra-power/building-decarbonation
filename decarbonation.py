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
st.sidebar.write("---")
st.sidebar.write("📈 **Étape 4 : Consommation énergétique**")

profil_conso = st.sidebar.selectbox(
    "Type de bâtiment",
    ["Résidentiel", "Tertiaire / Bureaux", "Industriel"]
)

mode_conso = st.sidebar.radio(
    "Données de consommation",
    ["Saisie manuelle (kWh)", "Upload courbe de charge"],
    horizontal=True
)

conso_annuelle_kwh = 0
df_courbe_charge = None

if mode_conso == "Saisie manuelle (kWh)":
    conso_annuelle_kwh = st.sidebar.number_input(
        "Consommation annuelle totale (kWh)",
        min_value=0,
        value=5000,
        step=100
    )
else:
    fichier_conso = st.sidebar.file_uploader(
        "Uploader votre courbe de charge (CSV ou Excel)",
        type=["csv", "xlsx"]
    )
    if fichier_conso:
        try:
            if fichier_conso.name.endswith('.csv'):
                df_courbe_charge = pd.read_csv(fichier_conso)
            else:
                df_courbe_charge = pd.read_excel(fichier_conso)
            st.sidebar.success("✅ Fichier chargé avec succès")
            # Ici il faudra ajouter une logique pour identifier les colonnes de temps et de puissance
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
        ecartement_calcule = 0

        import math

        for pan in donnees_pans:
            incli_pan = pan['inclinaison']
            surf_pan = pan['surface']
            orient_pan = pan['orientation']
            
            # 1. Calcul de la surface réelle après correctif de pente
            if mode_mesure == "Vue aérienne":
                surf_reelle = surf_pan / math.cos(math.radians(incli_pan))
            else:
                surf_reelle = surf_pan

            # 2. Déduction de la zone ERP (90cm sur tout le pourtour)
            cote_theorique = math.sqrt(surf_reelle)
            if cote_theorique > 1.8:
                surf_utile = (cote_theorique - 2 * pourtour_erp)**2
            else:
                surf_utile = 0
            
            surf_utile = max(0, surf_utile)

            # 3. Calcul de l'encombrement par module
            if type_toit == "Plat":
                # MODULES EN PAYSAGE
                dim_long = longueur_base + espacement_fixation
                dim_larg = largeur_base + espacement_fixation
                
                # Largeur projetée au sol (inclinaison 10°)
                larg_projetee = dim_larg * math.cos(math.radians(10))
                
                # Définition de l'allée selon la variante
                if "Est-Ouest" in variante_plat:
                    ecartement_optimal = 0.15 # 15 cm pour Est-Ouest
                else:
                    ecartement_optimal = 0.45 # 45 cm pour Sud
                
                surf_par_module = dim_long * (larg_projetee + ecartement_optimal)
                ecartement_calcule = ecartement_optimal
            else:
                # MODULES EN PORTRAIT
                dim_long = longueur_base + espacement_fixation
                dim_larg = largeur_base + espacement_fixation
                surf_par_module = dim_long * dim_larg

            # 4. Nombre de modules sur la surface utile
            nb_mods = int(surf_utile / surf_par_module)
            puissance_pan = nb_mods * 0.5 # 500Wc
            
            # Appel PVGIS
            aspect = get_aspect(orient_pan)
            prod_unit = appeler_pvgis(lat, lon, incli_pan, aspect)
            prod_mensuelle_unitaire = appeler_pvgis_mensuel(lat, lon, incli_pan, aspect)
            
            if prod_unit:
                nb_modules_total += nb_mods
                puissance_pv_installable += puissance_pan
                production_totale_an += puissance_pan * prod_unit
                
                if prod_mensuelle_unitaire:
                    for i in range(12):
                        prod_mensuelle_cumulee[i] += prod_mensuelle_unitaire[i] * puissance_pan

                details_pans_calcul.append({
                    "orientation": orient_pan,
                    "puissance": puissance_pan,
                    "prod_unit": prod_unit,
                    "nb_mods": nb_mods
                })

        # Limitation par la puissance d'introduction
        puissance_retenue = min(puissance_pv_installable, puissance_intro_kw)
        # Si limitation, on réduit proportionnellement la production
        if puissance_pv_installable > 0:
            facteur_limite = puissance_retenue / puissance_pv_installable
            production_totale_an *= facteur_limite
            prod_mensuelle_cumulee = [p * facteur_limite for p in prod_mensuelle_cumulee]
        
        productible_moyen = production_totale_an / puissance_retenue if puissance_retenue > 0 else 0
    else:
        productible_moyen = 0
        details_pvgis = []
        lat, lon = None, None
        puissance_pv_installable = 0
        puissance_retenue = 0
        production_totale_an = 0

    # --- AFFICHAGE DES RÉSULTATS ---
    st.header("Analyse du potentiel de décarbonation")

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
            
            # Création du DataFrame avec un index catégoriel pour forcer l'ordre
            df_mensuel = pd.DataFrame({
                "Mois": mois_noms,
                "Production (kWh)": prod_mensuelle_cumulee
            })
            df_mensuel["Mois"] = pd.Categorical(df_mensuel["Mois"], categories=mois_noms, ordered=True)
            
            # Utilisation de Plotly pour un contrôle total sur l'ordre des axes
            import plotly.express as px
            fig = px.bar(
                df_mensuel, 
                x="Mois", 
                y="Production (kWh)",
                color_discrete_sequence=["#FF4B4B"]
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

            st.write(f"**Toiture :** {type_toit} ({materiau})")
            if type_toit == "Plat":
                st.write(f"*(Allées d'entretien calculées : {ecartement_calcule*100:.0f} cm)*")
            st.write(f"**Potentiel toiture :** {puissance_pv_installable:.1f} kWc")
            st.write("**Détail par orientation :**")
            for d in details_pans_calcul:
                st.write(f"- {d['orientation']} : {d['puissance']:.1f} kWc ({d['nb_mods']} modules)")
        
# Affichage du graphique de production mensuelle PVGIS - RETIRÉ DU BAS
        # st.write("---")
        # ...
else:
    st.info("En attente d'une adresse valide pour calculer le productible via PVGIS.")
