from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from models import User, db
from werkzeug.security import check_password_hash, generate_password_hash

import config

auth = Blueprint("auth", __name__)


def login_required(role=None):
    def decorator(f):
        def decorated_function(*args, **kwargs):
            if "user_id" not in session:
                flash("Veuillez vous connecter", "danger")
                return redirect(url_for("auth.login"))
            if role and session.get("role") != role:
                flash("Accès non autorisé", "danger")
                if session.get("role") == "gardien":
                    return redirect(url_for("live"))
                return redirect(url_for("index"))
            return f(*args, **kwargs)

        decorated_function.__name__ = f.__name__
        return decorated_function

    return decorator


@auth.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""

        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.password, password):
            if not getattr(user, "is_active", True):
                flash("Ce compte est désactivé. Contactez l'administrateur.", "danger")
                return render_template("login.html")
            session["user_id"] = user.id
            session["username"] = user.username
            session["role"] = user.role
            session["site"] = user.site
            session["full_name"] = user.full_name

            if user.role != "admin":
                flash(f"Bienvenue {user.full_name or user.username} !", "success")

            if user.role == "gardien" and getattr(user, "must_change_password", False):
                flash("Veuillez définir un nouveau mot de passe personnel.", "warning")
                return redirect(url_for("auth.change_password"))

            if user.role == "admin":
                return redirect(url_for("index"))
            return redirect(url_for("live"))
        flash("Identifiants incorrects", "danger")

    return render_template("login.html")


@auth.route("/logout")
def logout():
    session.clear()
    flash("Vous avez été déconnecté", "info")
    return redirect(url_for("auth.login"))


@auth.route("/mot-de-passe-oublie", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        recovery = (request.form.get("recovery_code") or "").strip()
        new_password = (request.form.get("new_password") or "").strip()

        user = User.query.filter_by(username=username, role="admin").first()
        if not user:
            flash("Compte administrateur introuvable.", "danger")
            return render_template("forgot_password.html")
        if recovery != config.ADMIN_RECOVERY_CODE:
            flash("Code de récupération incorrect.", "danger")
            return render_template("forgot_password.html")
        if len(new_password) < 6:
            flash("Mot de passe trop court (minimum 6 caractères).", "warning")
            return render_template("forgot_password.html")

        user.password = generate_password_hash(new_password)
        db.session.commit()
        flash("Mot de passe administrateur réinitialisé. Vous pouvez vous connecter.", "success")
        return redirect(url_for("auth.login"))

    return render_template("forgot_password.html")


@auth.route("/changer-mot-de-passe", methods=["GET", "POST"])
@login_required()
def change_password():
    if request.method == "POST":
        current = request.form.get("current_password") or ""
        new_pw = (request.form.get("new_password") or "").strip()
        confirm = (request.form.get("confirm_password") or "").strip()

        user = User.query.get(session["user_id"])
        if not user:
            return redirect(url_for("auth.login"))

        if not check_password_hash(user.password, current):
            flash("Mot de passe actuel incorrect.", "danger")
            return render_template("change_password.html")

        if len(new_pw) < 4:
            flash("Nouveau mot de passe trop court.", "warning")
            return render_template("change_password.html")
        if new_pw != confirm:
            flash("Les mots de passe ne correspondent pas.", "warning")
            return render_template("change_password.html")

        user.password = generate_password_hash(new_pw)
        user.must_change_password = False
        db.session.commit()
        flash("Mot de passe mis à jour.", "success")
        if session.get("role") == "admin":
            return redirect(url_for("index"))
        return redirect(url_for("live"))

    return render_template("change_password.html")
