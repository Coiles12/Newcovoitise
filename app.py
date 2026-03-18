from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from functools import wraps
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
import qrcode
import io
import base64
from datetime import datetime, timedelta, date
import pronotepy
import json
import os
import random
import string
import secrets
import time
import requests
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

# Charge les variables du fichier .env (sécurité)
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "cle_par_defaut_insecure_si_pas_de_env")
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///covoit.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# ==========================================
#           CONFIGURATION & FICHIERS
# ==========================================
PRONOTE_URL = "https://0560181t.index-education.net/PRONOTE/eleve.html"
PRONOTE_USER = os.getenv("PRONOTE_USER")
PRONOTE_MDP = os.getenv("PRONOTE_MDP")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1483780868014342194/aBBCnAedJVuDKu_zU8KlE13J4QdsBC0j8M0Y7JHIzz7CcewZwMeVbFnCDyu-moQjrOxm")

CACHE_FILE = "cache_edt.json"
USERS_FILE = "users.json"
DEMAND_FILE = "demand_coefs.json"
MATIERES_IGNOREES = ["Foyer", "Permanence", "Etude", "Vie de classe", "Rattrapage"]

# --- PARAMETRES ECONOMIE ---
VALEUR_RECHARGE_HEBDO = 80   # Couvre 8 trajets "malins" (4 places / 5 personnes)
PLAFOND_CREDITS_MAX = 160    # 2 semaines de stock
CREDITS_DEPART = 100         # Un peu de marge au début

# --- GESTION CACHE & FICHIERS ---
CACHE_RAM = None
DERNIERE_MODIF_CACHE = 0
LAST_DISCORD_ALERT_DATE = None

def envoyer_notification_discord(message):
    """Envoie un message via Webhook Discord si l'URL est configurée."""
    if DISCORD_WEBHOOK_URL:
        try:
            requests.post(DISCORD_WEBHOOK_URL, json={"content": message})
        except Exception as e:
            print(f"Erreur Discord: {e}")

def generer_mdp_aleatoire(longueur=6):
    lettres = string.ascii_letters + string.digits
    return ''.join(random.choice(lettres) for i in range(longueur))

def charger_cache():
    global CACHE_RAM, DERNIERE_MODIF_CACHE
    if not os.path.exists(CACHE_FILE):
        return {}
    infos_fichier = os.stat(CACHE_FILE)
    date_modif = infos_fichier.st_mtime
    if CACHE_RAM is not None and date_modif == DERNIERE_MODIF_CACHE:
        return CACHE_RAM
    try:
        with open(CACHE_FILE, 'r') as f:
            CACHE_RAM = json.load(f)
            DERNIERE_MODIF_CACHE = date_modif
            return CACHE_RAM
    except:
        return {}

def charger_users_json():
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, 'r') as f:
                return json.load(f)
        except:
            return []
    return []

def sauvegarder_users_json(liste_users):
    with open(USERS_FILE, 'w') as f:
        json.dump(liste_users, f, indent=4)

def ajouter_user_au_json(pseudo, password, credits):
    users = charger_users_json()
    for u in users:
        if u['pseudo'] == pseudo:
            return False
    users.append({"pseudo": pseudo, "password": password, "credits": credits})
    sauvegarder_users_json(users)
    return True

def mettre_a_jour_mdp_json(pseudo, new_password):
    """Met à jour le mot de passe dans le JSON pour persistance"""
    users = charger_users_json()
    found = False
    for u in users:
        if u['pseudo'] == pseudo:
            u['password'] = new_password
            found = True
            break
    if found:
        sauvegarder_users_json(users)
    return found

# --- GESTION COEFFICIENTS DEMANDE ---
def charger_demand_coefs():
    if os.path.exists(DEMAND_FILE):
        try:
            with open(DEMAND_FILE, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}

def sauvegarder_demand_coefs(coefs):
    with open(DEMAND_FILE, 'w') as f:
        json.dump(coefs, f, indent=4)

def get_demand_coef(date_obj, sens):
    """Récupère le coef configuré pour ce jour de la semaine (0-6) et ce moment (Matin/Soir)"""
    coefs = charger_demand_coefs()
    jour_semaine = str(date_obj.weekday()) # 0=Lundi, 6=Dimanche
    cle = f"{jour_semaine}_{sens}" # ex: "0_Aller" ou "3_Retour"
    return float(coefs.get(cle, 1.0))

# ==========================================
#           PRONOTE
# ==========================================
def mettre_a_jour_cache_pronote():
    print("🔄 MISE À JOUR DU CACHE PRONOTE...")
    nouveau_cache = charger_cache()
    try:
        # Vérification si les identifiants sont présents
        if not PRONOTE_USER or not PRONOTE_MDP:
            print("⚠️ Identifiants Pronote manquants dans le fichier .env")
            return False

        client = pronotepy.Client(PRONOTE_URL, username=PRONOTE_USER, password=PRONOTE_MDP, uuid='')
        if client.logged_in:
            date_debut = date.today()
            date_fin = date_debut + timedelta(days=30)
            lessons = client.lessons(date_debut, date_fin)
            for i in range(31):
                jour_check = (date_debut + timedelta(days=i)).strftime("%Y-%m-%d")
                if jour_check not in nouveau_cache:
                    nouveau_cache[jour_check] = {"aller": "Pas de cours", "retour": "Pas de cours"}
            if lessons:
                lecons_par_jour = {}
                for l in lessons:
                    d_str = l.start.strftime("%Y-%m-%d")
                    if d_str not in lecons_par_jour:
                        lecons_par_jour[d_str] = []
                    lecons_par_jour[d_str].append(l)
                for d_str, cours_du_jour in lecons_par_jour.items():
                    cours_valides = []
                    for c in cours_du_jour:
                        if c.canceled: continue
                        est_ignore = False
                        for matiere_bannie in MATIERES_IGNOREES:
                            if matiere_bannie.lower() in c.subject.name.lower():
                                est_ignore = True
                                break
                        if not est_ignore:
                            cours_valides.append(c)
                    if not cours_valides:
                        nouveau_cache[d_str] = {"aller": "Pas de cours", "retour": "Pas de cours"}
                        continue
                    cours_valides.sort(key=lambda x: x.start)
                    heure_aller = cours_valides[0].start.strftime("%H:%M")
                    heure_retour = cours_valides[-1].end.strftime("%H:%M")
                    nouveau_cache[d_str] = {"aller": heure_aller, "retour": heure_retour}
            with open(CACHE_FILE, 'w') as f:
                json.dump(nouveau_cache, f, indent=4)
            return True
    except Exception as e:
        print(f"❌ Erreur Pronote: {e}")
        return False
    return False

def get_heure_depuis_cache(date_str, sens):
    cache = charger_cache()
    if date_str not in cache:
        if mettre_a_jour_cache_pronote():
            cache = charger_cache()
        else:
            return "Erreur Connexion"
    if date_str in cache:
        return cache[date_str]['aller'] if sens == 'aller' else cache[date_str]['retour']
    return "Donnée introuvable"

def get_jours_options():
    options = []
    jours_fr = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
    aujourdhui = date.today()
    for i in range(8):
        d = aujourdhui + timedelta(days=i)
        valeur = d.strftime("%Y-%m-%d")
        affichage = f"{jours_fr[d.weekday()]} {d.strftime('%d/%m')}"
        options.append({"valeur": valeur, "affichage": affichage})
    return options

# ==========================================
#           LOGIQUE PRIX
# ==========================================
def calculer_prix_dynamique(date_str, sens, seat, option_dj):
    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
        today = date.today()
        delta_days = (date_obj - today).days

        # Prix de base
        base = 10
        majoration_temps = 0

        if delta_days <= 0:  # Jour même (Urgence absolue)
            majoration_temps = 10
        elif delta_days == 1: # Veille
            majoration_temps = 5
        elif delta_days == 2: # Avant-veille (Le moins cher)
            majoration_temps = 0
        else:
            majoration_temps = min((delta_days - 2) * 2, 14)

        prix_intermediaire = base + majoration_temps

        # Application du Coef Admin
        cle_sens = "Aller" if sens.lower() == "aller" else "Retour"
        coef = get_demand_coef(date_obj, cle_sens)

        prix_apres_coef = int(prix_intermediaire * coef)

        # Options fixes
        prix_siege = 10 if seat == 'RF' else 0
        prix_dj = 5 if option_dj else 0

        total = prix_apres_coef + prix_siege + prix_dj

        return total, coef
    except Exception as e:
        print(f"Erreur calcul prix: {e}")
        return 10, 1.0

# ==========================================
#           MODÈLES DB
# ==========================================
ARRÊTS = ["Place de Bretagne", "Centre de Ploemeur", "Fontaine St Pierre", "Rond Point - Rue des plages", " Charles de Gaulle", "Autre"]

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    pseudo = db.Column(db.String(50), unique=True, index=True)
    password = db.Column(db.String(200))
    credits = db.Column(db.Integer, default=CREDITS_DEPART)
    first_login = db.Column(db.Boolean, default=True)
    last_refill = db.Column(db.DateTime, default=datetime(2000, 1, 1))
    is_admin = db.Column(db.Boolean, default=False)
    theme = db.Column(db.String(20), default='auto')
    default_arret = db.Column(db.String(100), default='')

class Ride(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, index=True)
    pseudo_passager = db.Column(db.String(50))
    seat = db.Column(db.String(20))
    nom_arret = db.Column(db.String(100))
    type_trajet = db.Column(db.String(20))
    jour_str = db.Column(db.String(50), index=True)
    heure_trajet = db.Column(db.String(20))
    options = db.Column(db.String(100))
    qr_data = db.Column(db.Text)
    cout_total = db.Column(db.Integer)
    date_trajet_reelle = db.Column(db.String(20))
    token_secret = db.Column(db.String(64), unique=True, index=True)
    est_valide = db.Column(db.Boolean, default=False)
    date_creation = db.Column(db.DateTime, default=datetime.utcnow)

class Ticket(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer)
    pseudo = db.Column(db.String(50))
    message = db.Column(db.Text)
    type_ticket = db.Column(db.String(20))
    date_creation = db.Column(db.DateTime, default=datetime.utcnow)

class History(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, index=True)
    pseudo = db.Column(db.String(50))
    type_trajet = db.Column(db.String(20))
    seat = db.Column(db.String(20))
    statut = db.Column(db.String(20))
    date_trajet = db.Column(db.Date)
    cout = db.Column(db.Integer)
    date_enregistrement = db.Column(db.DateTime, default=datetime.utcnow)

def archiver_trajet(ride, statut_final):
    try:
        date_obj = datetime.strptime(ride.date_trajet_reelle, "%Y-%m-%d").date()
        entry = History(
            user_id=ride.user_id,
            pseudo=ride.pseudo_passager,
            type_trajet=ride.type_trajet,
            seat=ride.seat,
            statut=statut_final,
            date_trajet=date_obj,
            cout=ride.cout_total if ride.cout_total else 10
        )
        db.session.add(entry)
        db.session.commit()
    except Exception as e:
        print(f"Erreur archivage: {e}")

def sync_users_db():
    users_json = charger_users_json()
    for u in users_json:
        user_db = User.query.filter_by(pseudo=u['pseudo']).first()
        if not user_db:
            hashed_pw = generate_password_hash(u['password'])
            c = u.get('credits', CREDITS_DEPART)
            db.session.add(User(pseudo=u['pseudo'], password=hashed_pw, credits=c, first_login=True))

    gustave = User.query.filter_by(pseudo='Gustave').first()
    if not gustave:
        hashed_admin_pw = generate_password_hash('GusLe.056')
        db.session.add(User(pseudo='Gustave', password=hashed_admin_pw, credits=9999, first_login=True, is_admin=True))
        ajouter_user_au_json('Gustave', 'GusLe.056', 9999)
    elif not gustave.is_admin:
        gustave.is_admin = True

    db.session.commit()

# ==========================================
#           LOGIQUE VENDREDI 17H
# ==========================================
def check_weekly_refill(user):
    now = datetime.now()
    jours_a_reculer = (now.weekday() - 4) % 7
    dernier_vendredi = now - timedelta(days=jours_a_reculer)
    cible_refill = dernier_vendredi.replace(hour=17, minute=0, second=0, microsecond=0)

    if cible_refill > now:
        cible_refill -= timedelta(days=7)

    if not user.last_refill or user.last_refill < cible_refill:
        bonus = VALEUR_RECHARGE_HEBDO
        plafond_max = PLAFOND_CREDITS_MAX
        ancien_solde = user.credits
        nouveau_solde = ancien_solde + bonus
        if nouveau_solde > plafond_max:
            nouveau_solde = plafond_max

        user.credits = nouveau_solde
        user.last_refill = now
        db.session.commit()
        return True, nouveau_solde

    return False, 0

with app.app_context():
    # Tente de mettre à jour l'ancienne base de données sans rien effacer
    try:
        db.session.execute(text('ALTER TABLE user ADD COLUMN is_admin BOOLEAN DEFAULT 0'))
        db.session.commit()
    except:
        db.session.rollback()
        
    try:
        db.session.execute(text("ALTER TABLE user ADD COLUMN theme VARCHAR(20) DEFAULT 'auto'"))
        db.session.commit()
    except:
        db.session.rollback()
        
    try:
        db.session.execute(text("ALTER TABLE user ADD COLUMN default_arret VARCHAR(100) DEFAULT ''"))
        db.session.commit()
    except:
        db.session.rollback()
        
    db.create_all()
    sync_users_db()

@app.context_processor
def inject_global_vars():
    if 'user_id' in session:
        return dict(current_user=User.query.get(session['user_id']), TOUS_LES_ARRETS=ARRÊTS)
    return dict(current_user=None, TOUS_LES_ARRETS=ARRÊTS)

# ==========================================
#              DÉCORATEURS
# ==========================================
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session or not session.get('is_admin'):
            flash("❌ Accès non autorisé.")
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

# ==========================================
#                 ROUTES
# ==========================================
@app.route('/update_settings', methods=['POST'])
def update_settings():
    if 'user_id' not in session: return redirect(url_for('index'))
    user = User.query.get(session['user_id'])
    user.theme = request.form.get('theme', 'auto')
    user.default_arret = request.form.get('default_arret', '')
    db.session.commit()
    flash("⚙️ Paramètres enregistrés !")
    return redirect(request.referrer or url_for('dashboard'))

@app.route('/')
def index():
    if 'user_id' in session: return redirect(url_for('dashboard'))
    return render_template('login.html')

@app.route('/login', methods=['POST'])
def login():
    pseudo = request.form.get('pseudo')
    password_input = request.form.get('password')
    user = User.query.filter_by(pseudo=pseudo).first()

    if user and check_password_hash(user.password, password_input):
        session['user_id'] = user.id
        session['pseudo'] = user.pseudo
        session['is_admin'] = user.is_admin
        if user.first_login: return redirect(url_for('setup_account'))
        return redirect(url_for('dashboard'))

    flash('Identifiant ou mot de passe incorrect')
    return redirect(url_for('index'))

@app.route('/request_account', methods=['POST'])
def request_account():
    pseudo = request.form.get('pseudo')
    if pseudo:
        if User.query.filter_by(pseudo=pseudo).first():
            flash("Ce pseudo a déjà un compte !")
        else:
            new_ticket = Ticket(user_id=0, pseudo=pseudo, type_ticket="Inscription", message="Veut rejoindre la Navette !")
            db.session.add(new_ticket)
            db.session.commit()
            flash("Demande envoyée au conducteur ! 📨")
    return redirect(url_for('index'))

@app.route('/setup-account', methods=['GET', 'POST'])
def setup_account():
    if 'user_id' not in session: return redirect(url_for('index'))
    user = User.query.get(session['user_id'])
    if not user.first_login: return redirect(url_for('dashboard'))

    if request.method == 'POST':
        new_pass = request.form.get('new_password')
        confirm_pass = request.form.get('confirm_password')

        if len(new_pass) < 4:
            flash("Mot de passe trop court !")
            return redirect(url_for('setup_account'))

        if new_pass != confirm_pass:
            flash("Les mots de passe ne correspondent pas !")
            return redirect(url_for('setup_account'))

        user.password = generate_password_hash(new_pass)
        user.first_login = False
        db.session.commit()

        # SAUVEGARDE JSON (PERSISTANCE)
        mettre_a_jour_mdp_json(user.pseudo, new_pass)

        flash("Compte configuré avec succès ! ✅")
        return redirect(url_for('dashboard'))
    return render_template('setup_account.html', user=user)

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session: return redirect(url_for('index'))
    user = User.query.get(session['user_id'])

    if not user:
        session.clear()
        return redirect(url_for('index'))
    if user.first_login: return redirect(url_for('setup_account'))

    a_ete_recharge, montant = check_weekly_refill(user)
    if a_ete_recharge:
        flash(f"📅 C'est vendredi ! Tes crédits sont rechargés (Total : {montant} 🪙)")

    # MODIFICATION : On ne montre que les trajets NON validés
    mes_trajets = Ride.query.filter_by(user_id=user.id, est_valide=False).order_by(Ride.id.desc()).all()

    now = datetime.now()
    if now.month >= 9:
        start_school = datetime(now.year, 9, 1)
        end_school = datetime(now.year + 1, 7, 5)
    else:
        start_school = datetime(now.year - 1, 9, 1)
        end_school = datetime(now.year, 7, 5)

    total_year_time = (end_school - start_school).total_seconds()
    elapsed_year = (now - start_school).total_seconds()
    prog_annee = max(0, min(100, (elapsed_year / total_year_time) * 100))

    jour_actuel = now.weekday()
    heure_actuelle = now.hour + now.minute/60
    if jour_actuel >= 5:
        prog_semaine = 100
    else:
        heures_passees = (jour_actuel * 24) + heure_actuelle
        total_semaine_target = (4 * 24) + 17
        prog_semaine = max(0, min(100, (heures_passees / total_semaine_target) * 100))

    return render_template('dashboard.html',
                           user=user,
                           trajets=mes_trajets,
                           prog_semaine=round(prog_semaine, 1),
                           prog_annee=round(prog_annee, 1))

def calculer_remboursement(ride):
    date_trajet = datetime.strptime(ride.date_trajet_reelle, "%Y-%m-%d").date()
    date_heure_trajet = datetime.combine(date_trajet, datetime.min.time()) + timedelta(hours=8)
    delta = date_heure_trajet - datetime.now()
    heures_restantes = delta.total_seconds() / 3600

    cout = ride.cout_total if ride.cout_total else 10
    
    if heures_restantes > 48:
        return cout, "Remboursement intégral"
    elif heures_restantes > 24:
        return int(cout * 0.75), "Remboursement partiel (75%)"
    elif heures_restantes > 2:
        return int(cout * 0.50), "Remboursement partiel (50%)"
    else:
        return int(cout * 0.25), "Remboursement minimum (25%)"

@app.route('/cancel_ride/<int:ride_id>')
def cancel_ride(ride_id):
    if 'user_id' not in session: return redirect(url_for('index'))
    user = User.query.get(session['user_id'])
    ride = Ride.query.get_or_404(ride_id)

    if ride.user_id != user.id:
        flash("Ce n'est pas ton trajet !")
        return redirect(url_for('dashboard'))

    if ride.est_valide:
        flash("Impossible d'annuler un trajet validé !")
        return redirect(url_for('dashboard'))

    remboursement, msg = calculer_remboursement(ride)
    return render_template('cancel_confirm.html', ride=ride, remboursement=remboursement, message_remboursement=msg)

@app.route('/cancel_ride_confirm/<int:ride_id>', methods=['POST'])
def cancel_ride_confirm(ride_id):
    if 'user_id' not in session: return redirect(url_for('index'))
    user = User.query.get(session['user_id'])
    ride = Ride.query.get_or_404(ride_id)

    if ride.user_id != user.id or ride.est_valide:
        return redirect(url_for('dashboard'))

    try:
        archiver_trajet(ride, "ANNULÉ")
        remboursement, msg = calculer_remboursement(ride)

        user.credits += remboursement
        db.session.delete(ride)
        db.session.commit()
        
        envoyer_notification_discord(f"🚫 **Trajet annulé** par {user.pseudo}.\n📅 {ride.jour_str} ({ride.type_trajet})\n🪙 Remboursé : {remboursement} crédits.")
        
        flash(f"🚫 Trajet annulé. {msg} (+{remboursement} 🪙)")
    except Exception as e:
        print(e)
        flash("Erreur lors de l'annulation")

    return redirect(url_for('dashboard'))

@app.route('/book', methods=['GET', 'POST'])
def book():
    if 'user_id' not in session: return redirect(url_for('index'))
    user = User.query.get(session['user_id'])
    if user.first_login: return redirect(url_for('setup_account'))

    if request.method == 'POST':
        seat = request.form.get('seat')
        arret = request.form.get('arret')
        date_valeur = request.form.get('date_valeur')
        sens = request.form.get('sens')
        option_dj = 'dj' in request.form

        # 1. VERIFICATION COMPLETE
        if not seat or not arret or not date_valeur or not sens:
            flash("❌ Formulaire incomplet ! Vérifie siège, date, sens et arrêt.")
            return redirect(url_for('book'))

        heure_trouvee = get_heure_depuis_cache(date_valeur, sens)
        if heure_trouvee in ["Pas de cours", "Pas de service", "Erreur Connexion", "Donnée introuvable"]:
            flash(f"Impossible : {heure_trouvee}")
            return redirect(url_for('book'))

        date_obj = datetime.strptime(date_valeur, "%Y-%m-%d").date()
        jours_fr = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
        jour_joli = f"{jours_fr[date_obj.weekday()]} {date_obj.strftime('%d/%m')}"

        # 2. VERIFICATION SIEGE DEJA PRIS
        deja_pris = Ride.query.filter_by(jour_str=jour_joli, type_trajet=sens.capitalize(), seat=seat).first()
        if deja_pris:
            flash(f"Trop tard ! Siège {seat} déjà réservé.")
            return redirect(url_for('book'))

        # 3. VERIFICATION DJ UNIQUE
        if option_dj:
            dj_pris = Ride.query.filter(Ride.jour_str==jour_joli, Ride.type_trajet==sens.capitalize(), Ride.options.contains('DJ')).first()
            if dj_pris:
                flash("L'option DJ est déjà prise par quelqu'un d'autre !")
                return redirect(url_for('book'))

        # Calcul Prix
        total, coef = calculer_prix_dynamique(date_valeur, sens, seat, option_dj)

        if user.credits < total:
            flash(f"Pas assez de crédits ({total} requis)")
            return redirect(url_for('book'))

        user.credits -= total

        token = secrets.token_hex(16)
        img = qrcode.make(token)
        data = io.BytesIO()
        img.save(data, "PNG")
        encoded_qr = base64.b64encode(data.getvalue()).decode('utf-8')

        new_ride = Ride(
            user_id=user.id, pseudo_passager=user.pseudo, seat=seat, nom_arret=arret,
            type_trajet=sens.capitalize(), jour_str=jour_joli, heure_trajet=heure_trouvee,
            options="DJ" if option_dj else "", qr_data=encoded_qr,
            cout_total=total, date_trajet_reelle=date_valeur,
            token_secret=token, est_valide=False
        )
        db.session.add(new_ride)
        db.session.commit()

        envoyer_notification_discord(f"🚗 **Nouveau trajet réservé !**\n👤 {user.pseudo}\n📅 {jour_joli} à {heure_trouvee} ({sens.capitalize()})\n💺 Place {seat} | 📍 {arret}")

        # MODIFICATION : Redirection au lieu de rendu direct pour éviter le "Refresh = Re-payer"
        return redirect(url_for('view_ticket', ride_id=new_ride.id))

    return render_template('book.html', user=user, arrets=ARRÊTS, jours=get_jours_options())

@app.route('/api/validate_scan', methods=['POST'])
@admin_required
def validate_scan():
    data = request.get_json()
    token = data.get('token')
    ride = Ride.query.filter_by(token_secret=token).first()

    if not ride:
        return jsonify({"status": "error", "message": "❌ TICKET INCONNU OU FALSIFIÉ"})
    if ride.est_valide:
        return jsonify({"status": "error", "message": f"⚠️ DÉJÀ VALIDÉ ! ({ride.pseudo_passager})"})

    ride.est_valide = True
    archiver_trajet(ride, "VALIDÉ")
    db.session.commit()
    return jsonify({"status": "success", "message": f"✅ Validé : {ride.pseudo_passager}"})

@app.route('/admin/validate_manual/<int:ride_id>')
@admin_required
def validate_manual(ride_id):
    ride = Ride.query.get(ride_id)
    if ride and not ride.est_valide:
        ride.est_valide = True
        archiver_trajet(ride, "VALIDÉ")
        db.session.commit()
        flash(f"✅ Trajet de {ride.pseudo_passager} validé.")
    return redirect(url_for('admin'))

@app.route('/recap')
def recap():
    if 'user_id' not in session: return redirect(url_for('index'))
    user = User.query.get(session['user_id'])
    history = History.query.filter_by(user_id=user.id).all()

    if not history:
        flash("Pas assez de données pour le récap !")
        return redirect(url_for('dashboard'))

    total_trajets = len([h for h in history if h.statut == 'VALIDÉ'])
    total_annules = len([h for h in history if h.statut == 'ANNULÉ'])
    credits_depenses = sum([h.cout for h in history if h.statut == 'VALIDÉ'])
    seats = [h.seat for h in history if h.statut == 'VALIDÉ']
    place_preferee = max(set(seats), key=seats.count) if seats else "Aucune"
    km_parcourus = total_trajets * 15
    co2_economise = round(km_parcourus * 0.12, 1)

    return render_template('recap.html', user=user, total=total_trajets, annules=total_annules, credits=credits_depenses, seat=place_preferee, km=km_parcourus, co2=co2_economise)

@app.route('/submit_ticket', methods=['POST'])
def submit_ticket():
    if 'user_id' not in session: return redirect(url_for('index'))
    user = User.query.get(session['user_id'])
    type_ticket = request.form.get('type')
    message = request.form.get('message')
    if message:
        new_ticket = Ticket(user_id=user.id, pseudo=user.pseudo, type_ticket=type_ticket, message=message)
        db.session.add(new_ticket)
        db.session.commit()
        flash("Message envoyé ! 📩")
    return redirect(url_for('dashboard'))

@app.route('/admin/delete_ticket/<int:ticket_id>')
@admin_required
def delete_ticket(ticket_id):
    ticket = Ticket.query.get(ticket_id)
    if ticket:
        db.session.delete(ticket)
        db.session.commit()
        flash("Ticket supprimé.")
    return redirect(url_for('admin'))

@app.route('/admin/edit_horaire', methods=['POST'])
@admin_required
def edit_horaire():
    date_modif = request.form.get('date_modif')
    nouvel_aller = request.form.get('aller')
    nouveau_retour = request.form.get('retour')
    cache = charger_cache()
    if date_modif in cache:
        cache[date_modif]['aller'] = nouvel_aller
        cache[date_modif]['retour'] = nouveau_retour
        with open(CACHE_FILE, 'w') as f:
            json.dump(cache, f, indent=4)
        flash(f"✅ Horaire du {date_modif} mis à jour !")
    return redirect(url_for('admin'))

@app.route('/admin/update_demand', methods=['POST'])
@admin_required
def update_demand():
    coefs = charger_demand_coefs()
    for i in range(7):
        for sens in ["Aller", "Retour"]:
            key = f"{i}_{sens}"
            val = request.form.get(f"coef_{key}")
            if val:
                try:
                    coefs[key] = float(val)
                except:
                    pass
    sauvegarder_demand_coefs(coefs)
    flash("✅ Coefficients de demande mis à jour !")
    return redirect(url_for('admin'))

@app.route('/ticket/<int:ride_id>')
def view_ticket(ride_id):
    if 'user_id' not in session: return redirect(url_for('index'))
    ride = Ride.query.get_or_404(ride_id)
    if ride.user_id != session['user_id']: return redirect(url_for('dashboard'))
    return render_template('ticket.html', ride=ride, qr_code=ride.qr_data)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/admin', methods=['GET', 'POST'])
@admin_required
def admin():
    if request.method == 'POST':
        pseudo_new = request.form.get('new_pseudo')
        mdp_custom = request.form.get('new_password')
        mdp_final = mdp_custom if mdp_custom else generer_mdp_aleatoire()

        if User.query.filter_by(pseudo=pseudo_new).first():
            flash("❌ Pseudo pris !")
        else:
            hashed_pw = generate_password_hash(mdp_final)
            db.session.add(User(pseudo=pseudo_new, password=hashed_pw, credits=CREDITS_DEPART, first_login=True))
            db.session.commit()
            ajouter_user_au_json(pseudo_new, mdp_final, CREDITS_DEPART)
            flash(f"✅ {pseudo_new} créé ! MDP: {mdp_final}")

    # MODIFICATION : Séparation en deux listes
    rides_pending = Ride.query.filter_by(est_valide=False).order_by(Ride.id.desc()).all()
    # On limite l'historique validé pour ne pas faire laguer la page admin
    rides_validated = Ride.query.filter_by(est_valide=True).order_by(Ride.id.desc()).limit(50).all()

    tous_les_tickets = Ticket.query.order_by(Ticket.id.desc()).all()
    edt_cache = charger_cache()
    
    # NETTOYAGE DES DATES PASSÉES ET ALERTE
    aujourdhui_str = date.today().strftime("%Y-%m-%d")
    dates_a_supprimer = [d for d in edt_cache.keys() if d < aujourdhui_str]
    if dates_a_supprimer:
        for d in dates_a_supprimer:
            del edt_cache[d]
        with open(CACHE_FILE, 'w') as f:
            json.dump(edt_cache, f, indent=4)
            
    jours_restants = len(edt_cache.keys())
    global LAST_DISCORD_ALERT_DATE
    if jours_restants <= 7 and LAST_DISCORD_ALERT_DATE != aujourdhui_str:
        envoyer_notification_discord(f"⚠️ **Alerte Emploi du Temps !**\nIl ne te reste que **{jours_restants} jours** en cache. N'oublie pas de mettre à jour Pronote.")
        LAST_DISCORD_ALERT_DATE = aujourdhui_str
        
    edt_trie = dict(sorted(edt_cache.items()))
    users = User.query.all()
    demand_coefs = charger_demand_coefs()

    return render_template('admin.html',
                           rides_pending=rides_pending, # Modif variable
                           rides_validated=rides_validated, # Modif variable
                           edt=edt_trie,
                           users=users,
                           tickets=tous_les_tickets,
                           demand_coefs=demand_coefs,
                           days_names=["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"])

@app.route('/admin/force_update')
@admin_required
def force_update():
    if mettre_a_jour_cache_pronote():
        flash("✅ Cache mis à jour !")
    else:
        flash("❌ Échec")
    return redirect(url_for('admin'))

@app.route('/api/check_horaire', methods=['POST'])
def check_horaire():
    if 'user_id' not in session: return jsonify({"status": "error"})
    data = request.get_json()
    date_valeur = data.get('date')
    sens = data.get('sens')
    if not date_valeur or not sens: return jsonify({"status": "error"})

    heure = get_heure_depuis_cache(date_valeur, sens)

    try:
        date_obj = datetime.strptime(date_valeur, "%Y-%m-%d").date()
        jours_fr = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
        jour_joli = f"{jours_fr[date_obj.weekday()]} {date_obj.strftime('%d/%m')}"

        trajets_existants = Ride.query.filter_by(jour_str=jour_joli, type_trajet=sens.capitalize()).all()
        sieges_pris = [t.seat for t in trajets_existants]

        # Vérif si DJ pris
        dj_pris = any('DJ' in t.options for t in trajets_existants)

        # Calcul simulation prix pour affichage frontend
        prix_base, coef = calculer_prix_dynamique(date_valeur, sens, "XX", False)

    except Exception as e:
        print(e)
        sieges_pris = []
        dj_pris = False
        prix_base = 10
        coef = 1.0

    return jsonify({
        "status": "success",
        "heure": heure,
        "occupied": sieges_pris,
        "dj_taken": dj_pris,
        "base_price": prix_base,
        "coef": coef
    })

# AJOUT : Route historique
@app.route('/history')
def history():
    if 'user_id' not in session: return redirect(url_for('index'))
    user = User.query.get(session['user_id'])

    # On récupère tout l'historique trié du plus récent au plus vieux
    historique = History.query.filter_by(user_id=user.id).order_by(History.date_trajet.desc()).all()

    return render_template('history.html', user=user, history=historique)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000, threaded=True)