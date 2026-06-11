from __future__ import annotations

import io
import json
import os
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer

APP_TITLE = "CoopArchive"

GDRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]

DB_FILES = {
    "categories": "db_categories.json",
    "users":      "db_users.json",
    "documents":  "db_documents.json",
    "activities": "db_activities.json",
}

CATEGORY_STYLES = {
    "Gestion Administrative":            {"color": "#F59F00", "icon": "🪪"},
    "Traçabilité et prime":              {"color": "#F03E3E", "icon": "💰"},
    "Agriculture":                       {"color": "#2F9E44", "icon": "🌾"},
    "Social":                            {"color": "#105EAD", "icon": "👥"},
    "Environnement":                     {"color": "#10AD3F", "icon": "🌿"},
    "Certificats":                       {"color": "#7B2CBF", "icon": "🏅"},
    "Contrats":                          {"color": "#2C7BE5", "icon": "📄"},
    "Procédures et politiques internes": {"color": "#E8590C", "icon": "📋"},
    "Autres":                            {"color": "#6C757D", "icon": "🗂️"},
}

ACTION_COLORS = {
    "Ajout":          "#2F9E44",
    "Modification":   "#F08C00",
    "Suppression":    "#E03131",
    "Restauration":   "#1C7ED6",
    "Connexion":      "#6C757D",
    "Téléchargement": "#5F3DC4",
}

DEMO_USERS = [
    ("Joel ATTEKE", "joel@socoopacdi.local", "Administrateur", "Joel2026"),
]


# ─────────────────────────────────────────────
# GOOGLE DRIVE CLIENT
# ─────────────────────────────────────────────

@st.cache_resource
def get_gdrive_service():
    raw = st.secrets["GDRIVE_SERVICE_ACCOUNT"]
    sa_info = json.loads(raw) if isinstance(raw, str) else dict(raw)
    if "private_key" in sa_info:
        sa_info["private_key"] = sa_info["private_key"].replace("\\n", "\n")
    creds = service_account.Credentials.from_service_account_info(sa_info, scopes=GDRIVE_SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def gdrive_folder_id() -> str:
    return st.secrets["GDRIVE_FOLDER_ID"]


# ─────────────────────────────────────────────
# BASE DE DONNÉES JSON SUR DRIVE
# ─────────────────────────────────────────────

def _find_file_id(filename: str) -> str | None:
    """Cherche un fichier par nom dans le dossier Drive racine."""
    service = get_gdrive_service()
    folder_id = gdrive_folder_id()
    query = f"name='{filename}' and '{folder_id}' in parents and trashed=false"
    res = service.files().list(
        q=query,
        fields="files(id)",
        pageSize=1,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None


def _read_json_from_drive(filename: str) -> list:
    """Lit un fichier JSON depuis Drive et retourne une liste."""
    file_id = _find_file_id(filename)
    if not file_id:
        return []
    try:
        service = get_gdrive_service()
        request = service.files().get_media(fileId=file_id)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return json.loads(buf.getvalue().decode("utf-8"))
    except Exception:
        return []


def _write_json_to_drive(filename: str, data: list) -> None:
    """Écrit/remplace un fichier JSON dans Drive."""
    service = get_gdrive_service()
    folder_id = gdrive_folder_id()
    content = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    media = MediaIoBaseUpload(
        io.BytesIO(content), mimetype="application/json", resumable=False
    )
    existing_id = _find_file_id(filename)
    if existing_id:
        service.files().update(
            fileId=existing_id,
            media_body=media,
            supportsAllDrives=True,
        ).execute()
   else:
        metadata = {"name": filename, "parents": [folder_id]}
        service.files().create(
            body=metadata,
            media_body=media,
            fields="id",
        ).execute()


def db_read(table: str) -> list:
    return _read_json_from_drive(DB_FILES[table])


def db_write(table: str, data: list) -> None:
    _write_json_to_drive(DB_FILES[table], data)


def db_insert(table: str, record: dict) -> dict:
    data = db_read(table)
    new_id = (max((r["id"] for r in data), default=0)) + 1
    record["id"] = new_id
    data.append(record)
    db_write(table, data)
    return record


def db_update(table: str, record_id: int, updates: dict) -> None:
    data = db_read(table)
    for r in data:
        if r["id"] == record_id:
            r.update(updates)
            break
    db_write(table, data)


def db_delete(table: str, record_id: int) -> None:
    data = db_read(table)
    data = [r for r in data if r["id"] != record_id]
    db_write(table, data)


# ─────────────────────────────────────────────
# STOCKAGE FICHIERS DRIVE
# ─────────────────────────────────────────────

def gdrive_upload(file_bytes: bytes, filename: str, mime_type: str) -> str:
    service = get_gdrive_service()
    folder_id = gdrive_folder_id()
    year_month = datetime.now().strftime("%Y/%m")
    subfolder_id = _gdrive_ensure_path(service, folder_id, year_month)
    metadata = {"name": filename, "parents": [subfolder_id]}
    media = MediaIoBaseUpload(
        io.BytesIO(file_bytes),
        mimetype=mime_type or "application/octet-stream",
        resumable=True,
    )
    result = service.files().create(
        body=metadata,
        media_body=media,
        fields="id",
    ).execute()


def gdrive_download(file_id: str) -> bytes | None:
    if not file_id:
        return None
    try:
        service = get_gdrive_service()
        request = service.files().get_media(fileId=file_id)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue()
    except Exception:
        return None


def gdrive_delete_file(file_id: str) -> None:
    if not file_id:
        return
    try:
        service = get_gdrive_service()
        service.files().delete(
            fileId=file_id,
            supportsAllDrives=True,
        ).execute()
    except Exception:
        pass


def _gdrive_ensure_path(service, root_id: str, path: str) -> str:
    parts = path.strip("/").split("/")
    current_id = root_id
    for part in parts:
        query = (
            f"name='{part}' and '{current_id}' in parents "
            f"and mimeType='application/vnd.google-apps.folder' and trashed=false"
        )
        res = service.files().list(
            q=query,
            fields="files(id)",
            pageSize=1,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files = res.get("files", [])
        if files:
            current_id = files[0]["id"]
        else:
            meta = {
                "name": part,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [current_id],
            }
            folder = service.files().create(
                body=meta,
                fields="id",
                supportsAllDrives=True,
            ).execute()
            current_id = folder["id"]
    return current_id


# ─────────────────────────────────────────────
# INITIALISATION BASE DE DONNÉES
# ─────────────────────────────────────────────

def initialize_database() -> None:
    # Vérification de l'accès au dossier Drive
    try:
        service = get_gdrive_service()
        folder_id = gdrive_folder_id()
        service.files().get(
            fileId=folder_id,
            fields="id,name",
            supportsAllDrives=True,
        ).execute()
    except Exception as e:
        st.error(f"❌ Dossier Google Drive inaccessible : {e}\n\n"
                 f"Vérifiez que le dossier est partagé avec l'email du Service Account (Éditeur) "
                 f"et que GDRIVE_FOLDER_ID est correct dans les secrets.")
        st.stop()

    try:
        cats = db_read("categories")
    except Exception as e:
        st.error(f"❌ Impossible d'accéder à Google Drive : {e}")
        st.stop()

    if not cats:
        for name, style in CATEGORY_STYLES.items():
            db_insert("categories", {
                "name": name,
                "color": style["color"],
                "icon": style["icon"],
                "created_at": current_timestamp(),
            })

    users = db_read("users")
    if not users:
        for name, email, role, password in DEMO_USERS:
            db_insert("users", {
                "name": name,
                "email": email,
                "role": role,
                "password": password,
                "active": True,
                "created_at": current_timestamp(),
            })

    if not db_read("documents"):
        db_write("documents", [])
    if not db_read("activities"):
        db_write("activities", [])


# ─────────────────────────────────────────────
# UTILITAIRES
# ─────────────────────────────────────────────

def current_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def format_datetime(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S").strftime("%d/%m/%Y %H:%M")
    except Exception:
        return str(value)


def format_date(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        return str(value)


def format_size(size_bytes) -> str:
    try:
        size = float(size_bytes)
    except Exception:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size_bytes} B"


def insert_activity(user_id, action: str, label: str, document_id=None, details: str = "") -> None:
    db_insert("activities", {
        "user_id": user_id,
        "action": action,
        "label": label,
        "document_id": document_id,
        "details": details,
        "created_at": current_timestamp(),
    })


def get_current_user_id():
    user = st.session_state.get("current_user")
    return user["id"] if user else None


# ─────────────────────────────────────────────
# REQUÊTES → DATAFRAMES
# ─────────────────────────────────────────────

def get_categories_df() -> pd.DataFrame:
    data = db_read("categories")
    return pd.DataFrame(data).sort_values("name").reset_index(drop=True) if data else pd.DataFrame()


def get_users_df() -> pd.DataFrame:
    data = db_read("users")
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    cols = [c for c in ["id", "name", "email", "role", "active", "created_at"] if c in df.columns]
    return df[cols].sort_values("name").reset_index(drop=True)


def get_documents_df(include_deleted: bool = False) -> pd.DataFrame:
    docs  = db_read("documents")
    cats  = db_read("categories")
    users = db_read("users")

    if not docs:
        return pd.DataFrame()

    df = pd.DataFrame(docs)

    if cats:
        cats_df = pd.DataFrame(cats).rename(columns={
            "id": "category_id", "name": "category_name",
            "color": "category_color", "icon": "category_icon"
        })
        df["category_id"] = pd.to_numeric(df.get("category_id", pd.Series()), errors="coerce")
        cats_df["category_id"] = pd.to_numeric(cats_df["category_id"], errors="coerce")
        df = df.merge(cats_df[["category_id", "category_name", "category_color", "category_icon"]], on="category_id", how="left")
    else:
        df["category_name"]  = ""
        df["category_color"] = "#6C757D"
        df["category_icon"]  = "🗂️"

    if users:
        users_df = pd.DataFrame(users).rename(columns={"id": "owner_id", "name": "owner_name", "role": "owner_role"})
        df["owner_id"]       = pd.to_numeric(df.get("owner_id", pd.Series()), errors="coerce")
        users_df["owner_id"] = pd.to_numeric(users_df["owner_id"], errors="coerce")
        df = df.merge(users_df[["owner_id", "owner_name", "owner_role"]], on="owner_id", how="left")
    else:
        df["owner_name"] = ""
        df["owner_role"] = ""

    df["size_bytes"] = pd.to_numeric(df.get("size_bytes", 0), errors="coerce").fillna(0).astype(int)
    df["is_deleted"] = df.get("is_deleted", False).astype(bool)

    if not include_deleted:
        df = df[df["is_deleted"] == False].copy()

    df = df.sort_values("created_at", ascending=False).reset_index(drop=True)
    if not df.empty:
        df["created_at_dt"] = pd.to_datetime(df["created_at"], errors="coerce")

    return df


def get_recent_activities_df(limit: int = 8) -> pd.DataFrame:
    acts  = db_read("activities")
    users = db_read("users")
    if not acts:
        return pd.DataFrame()

    df = pd.DataFrame(acts)
    if users:
        users_df = pd.DataFrame(users).rename(columns={"id": "user_id", "name": "user_name"})
        df["user_id"]        = pd.to_numeric(df.get("user_id", pd.Series()), errors="coerce")
        users_df["user_id"]  = pd.to_numeric(users_df["user_id"], errors="coerce")
        df = df.merge(users_df[["user_id", "user_name"]], on="user_id", how="left")
        df["user_name"] = df["user_name"].fillna("Système")
    else:
        df["user_name"] = "Système"

    return df.sort_values("created_at", ascending=False).head(limit).reset_index(drop=True)


# ─────────────────────────────────────────────
# OPÉRATIONS SUR LES DOCUMENTS
# ─────────────────────────────────────────────

def add_document(title: str, uploaded_file: Any, category_id: int,
                 owner_id: int, description: str, tags: str,
                 expires_at: date | None) -> None:
    original_name = uploaded_file.name
    suffix = "." + original_name.rsplit(".", 1)[-1].lower() if "." in original_name else ".bin"
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    stored_name = f"{timestamp}{suffix}"
    file_bytes = bytes(uploaded_file.getbuffer())
    mime = getattr(uploaded_file, "type", "application/octet-stream")
    gdrive_file_id = gdrive_upload(file_bytes, stored_name, mime)

    now = current_timestamp()
    result = db_insert("documents", {
        "title":             title.strip() or original_name,
        "original_filename": original_name,
        "stored_filename":   stored_name,
        "storage_path":      gdrive_file_id,
        "extension":         suffix,
        "mime_type":         mime,
        "category_id":       category_id,
        "owner_id":          owner_id,
        "description":       description.strip(),
        "tags":              tags.strip(),
        "size_bytes":        len(file_bytes),
        "expires_at":        expires_at.strftime("%Y-%m-%d") if expires_at else None,
        "status":            "Actif",
        "is_deleted":        False,
        "created_at":        now,
        "updated_at":        now,
        "deleted_at":        None,
    })
    insert_activity(owner_id, "Ajout", title.strip() or original_name, result["id"], description.strip())


def download_document_bytes(stored_filename: str, storage_path: str):
    file_id = storage_path if storage_path else stored_filename
    return gdrive_download(file_id)


def soft_delete_document(document_id: int, actor_id) -> None:
    now = current_timestamp()
    docs = db_read("documents")
    row = next((d for d in docs if d["id"] == document_id), None)
    if not row:
        return
    db_update("documents", document_id, {
        "is_deleted": True, "deleted_at": now, "status": "Archivé", "updated_at": now
    })
    insert_activity(actor_id, "Suppression", row["title"], document_id, "Document déplacé dans la corbeille.")


def restore_document(document_id: int, actor_id) -> None:
    now = current_timestamp()
    docs = db_read("documents")
    row = next((d for d in docs if d["id"] == document_id), None)
    if not row:
        return
    db_update("documents", document_id, {
        "is_deleted": False, "deleted_at": None, "status": "Actif", "updated_at": now
    })
    insert_activity(actor_id, "Restauration", row["title"], document_id, "Document restauré depuis la corbeille.")


def permanently_delete_document(document_id: int, actor_id) -> None:
    docs = db_read("documents")
    row = next((d for d in docs if d["id"] == document_id), None)
    if not row:
        return
    gdrive_delete_file(row.get("storage_path", ""))
    db_delete("documents", document_id)
    insert_activity(actor_id, "Suppression", row["title"], None, "Document supprimé définitivement.")


def update_document(document_id: int, title: str, description: str,
                    tags: str, expires_at: date | None, actor_id) -> None:
    db_update("documents", document_id, {
        "title":       title.strip(),
        "description": description.strip(),
        "tags":        tags.strip(),
        "expires_at":  expires_at.strftime("%Y-%m-%d") if expires_at else None,
        "updated_at":  current_timestamp(),
    })
    insert_activity(actor_id, "Modification", title.strip(), document_id, "Informations mises à jour.")


def add_category(name: str, color: str, icon: str) -> None:
    db_insert("categories", {
        "name": name.strip(), "color": color,
        "icon": icon.strip() or "🗂️", "created_at": current_timestamp(),
    })


def add_user(name: str, email: str, role: str, password: str = "changeme") -> None:
    result = db_insert("users", {
        "name": name.strip(), "email": email.strip(), "role": role.strip(),
        "password": password.strip(), "active": True, "created_at": current_timestamp(),
    })
    insert_activity(result["id"], "Connexion", name.strip(), None, "Utilisateur créé.")


def delete_user(user_id: int) -> None:
    users = db_read("users")
    row = next((u for u in users if u["id"] == user_id), None)
    if not row:
        return
    active = [u for u in users if u.get("active") and u["id"] != user_id]
    if not active:
        raise ValueError("Impossible de supprimer le dernier utilisateur actif.")
    db_delete("users", user_id)
    insert_activity(None, "Suppression", row["name"], None, "Utilisateur supprimé définitivement.")


def authenticate_user(email: str, password: str):
    users = db_read("users")
    for u in users:
        if (u.get("email", "").strip() == email.strip()
                and u.get("password", "").strip() == password.strip()
                and u.get("active")):
            return {"id": u["id"], "name": u["name"], "email": u["email"], "role": u["role"]}
    return None


# ─────────────────────────────────────────────
# MÉTRIQUES
# ─────────────────────────────────────────────

def expiring_documents_count(documents_df: pd.DataFrame, days: int = 30) -> int:
    if documents_df.empty or "expires_at" not in documents_df.columns:
        return 0
    today   = pd.Timestamp(date.today())
    expires = pd.to_datetime(documents_df["expires_at"], errors="coerce")
    delta   = (expires - today).dt.days
    return int(((delta >= 0) & (delta <= days)).sum())


def storage_stats(documents_df: pd.DataFrame) -> tuple[int, int]:
    used     = int(documents_df["size_bytes"].sum()) if not documents_df.empty else 0
    capacity = 15 * 1024 * 1024 * 1024
    return used, capacity


def dashboard_metrics(documents_df: pd.DataFrame) -> dict[str, Any]:
    today_str = date.today().strftime("%Y-%m-%d")
    total     = len(documents_df)
    added     = 0 if documents_df.empty else int((documents_df["created_at"].str[:10] == today_str).sum())
    cats      = int(documents_df["category_name"].nunique()) if not documents_df.empty else len(CATEGORY_STYLES)
    users     = len(get_users_df())
    used, _   = storage_stats(documents_df)
    return {
        "total_documents": total, "added_today": added, "categories": cats,
        "users_total": users, "used_storage": format_size(used),
    }


# ─────────────────────────────────────────────
# PDF EXPORT
# ─────────────────────────────────────────────

def generate_pdf_report(df: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        rightMargin=1.5 * cm, leftMargin=1.5 * cm,
        topMargin=2 * cm, bottomMargin=1.5 * cm,
    )
    styles     = getSampleStyleSheet()
    title_style    = ParagraphStyle("T", parent=styles["Heading1"], fontSize=18, textColor=colors.HexColor("#0F2B17"), spaceAfter=6)
    subtitle_style = ParagraphStyle("S", parent=styles["Normal"],   fontSize=10, textColor=colors.HexColor("#5C6B61"), spaceAfter=20)
    normal = styles["Normal"]
    story = [
        Paragraph("🗄️ CoopArchive — Rapport des documents", title_style),
        Paragraph(f"Généré le {datetime.now().strftime('%d/%m/%Y à %H:%M')} — SOCOOPACDI", subtitle_style),
        Paragraph(f"<b>Total documents :</b> {len(df)}", normal),
        Paragraph(f"<b>Catégories actives :</b> {df['category_name'].nunique()}", normal),
        Paragraph(f"<b>Taille totale :</b> {format_size(int(df['size_bytes'].sum()))}", normal),
        Paragraph(f"<b>Expirant sous 30 jours :</b> {expiring_documents_count(df)}", normal),
        Spacer(1, 0.5 * cm),
        Paragraph("<b>Répartition par catégorie</b>", styles["Heading2"]),
    ]
    cat_counts = df.groupby("category_name")["id"].count().reset_index()
    cat_counts.columns = ["Catégorie", "Nombre"]
    cat_data  = [["Catégorie", "Nombre de documents"]] + cat_counts.values.tolist()
    cat_table = Table(cat_data, colWidths=[10 * cm, 6 * cm])
    cat_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0F2B17")),
        ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
        ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#F3F7F4"), colors.white]),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D0D8D3")),
        ("ALIGN", (1, 0), (1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story += [cat_table, Spacer(1, 0.7 * cm), Paragraph("<b>Liste complète des documents</b>", styles["Heading2"])]
    export_df = df[["title", "category_name", "owner_name", "size_bytes", "status", "expires_at"]].copy()
    export_df["size_bytes"] = export_df["size_bytes"].map(format_size)
    export_df["expires_at"] = pd.to_datetime(export_df["expires_at"], errors="coerce").dt.strftime("%d/%m/%Y").fillna("—")
    export_df = export_df.rename(columns={
        "title": "Titre", "category_name": "Catégorie", "owner_name": "Propriétaire",
        "size_bytes": "Taille", "status": "Statut", "expires_at": "Expiration",
    })
    rows = [list(export_df.columns)] + export_df.values.tolist()
    doc_table = Table(rows, colWidths=[5.5 * cm, 3.5 * cm, 2.8 * cm, 2 * cm, 1.8 * cm, 2.5 * cm], repeatRows=1)
    doc_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2C7BE5")),
        ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
        ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#EEF5FB"), colors.white]),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#C8D8EC")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story += [
        doc_table, Spacer(1, 0.5 * cm),
        Paragraph(
            f"<i>Rapport généré automatiquement par CoopArchive · {date.today().strftime('%d/%m/%Y')}</i>",
            ParagraphStyle("F", parent=normal, fontSize=8, textColor=colors.HexColor("#8A9A8D")),
        ),
    ]
    doc.build(story)
    return buffer.getvalue()


# ─────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────

def card_html(title, value, subtitle, bg_color, icon):
    return f"""<div class="metric-card" style="background:{bg_color};">
        <div class="metric-icon">{icon}</div>
        <div class="metric-value">{value}</div>
        <div class="metric-title">{title}</div>
        <div class="metric-subtitle">{subtitle}</div>
    </div>"""


def inject_css():
    st.set_page_config(page_title=APP_TITLE, page_icon="🗄️", layout="wide")
    st.markdown("""<style>
    .stApp{background-color:#EEF2F4}
    [data-testid="stSidebar"]{background:linear-gradient(180deg,#0F2B17 0%,#123B1C 100%)}
    [data-testid="stSidebar"] *{color:#F5F7F8}
    .app-title{font-size:1.75rem;font-weight:700;color:#17321F;margin-bottom:.25rem}
    .app-subtitle{color:#5C6B61;margin-bottom:1rem}
    .metric-card{border-radius:18px;padding:1rem 1.1rem;color:white;min-height:130px;box-shadow:0 8px 18px rgba(0,0,0,.08)}
    .metric-icon{font-size:1.3rem;margin-bottom:.4rem}
    .metric-value{font-size:2rem;font-weight:700;line-height:1.1}
    .metric-title{font-weight:600;margin-top:.25rem}
    .metric-subtitle{opacity:.85;font-size:.9rem;margin-top:.25rem}
    .section-card{background:white;border-radius:18px;padding:1rem;box-shadow:0 4px 16px rgba(15,35,23,.06);border:1px solid rgba(15,35,23,.05);height:100%}
    .section-title{font-size:1.08rem;font-weight:700;color:#1E3122;margin-bottom:.75rem}
    .doc-row,.activity-row{padding:.55rem 0;border-bottom:1px solid #EEF2F4}
    .doc-row:last-child,.activity-row:last-child{border-bottom:none}
    .pill{display:inline-block;padding:.15rem .55rem;border-radius:999px;color:white;font-size:.78rem;font-weight:600}
    .small-muted{color:#6C757D;font-size:.85rem}
    .brand-box{background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.09);border-radius:18px;padding:1rem;margin-bottom:1rem}
    .brand-title{font-size:1.25rem;font-weight:700}
    .brand-subtitle{opacity:.8;font-size:.92rem}
    .login-card{background:#fff;border-radius:20px;padding:2.8rem 2.5rem 2rem;box-shadow:0 24px 64px rgba(0,0,0,.30);width:100%}
    .login-header{text-align:center;margin-bottom:1.8rem}
    .login-icon-ring{width:72px;height:72px;border-radius:50%;background:linear-gradient(135deg,#0F2B17,#2F9E44);display:flex;align-items:center;justify-content:center;font-size:2rem;margin:0 auto 1rem;box-shadow:0 8px 20px rgba(15,43,23,.35)}
    .login-app-name{font-size:1.8rem;font-weight:800;color:#0F2B17;letter-spacing:-.5px;margin-bottom:.2rem}
    .login-org{font-size:.88rem;color:#2F9E44;font-weight:600;letter-spacing:.04em;text-transform:uppercase;margin-bottom:.4rem}
    .login-tagline{font-size:.92rem;color:#6B7C73}
    .login-divider{height:1px;background:linear-gradient(90deg,transparent,#D0E8D8,transparent);margin:1.4rem 0}
    .login-footer{text-align:center;margin-top:1.4rem;font-size:.78rem;color:#9AA89E}
    .preview-box{background:#F8FAFB;border:1px solid #E2ECE5;border-radius:12px;padding:1rem;margin-bottom:.75rem}
    [data-testid="stTextInput"] input,[data-testid="stTextArea"] textarea{background-color:#E8F4FD!important;border:1.5px solid #2C7BE5!important;border-radius:8px!important;color:#0F2B17!important;font-weight:500!important}
    [data-testid="stTextInput"] input[type="password"]{background-color:#EEF4FB!important;border:1.5px solid #7B2CBF!important}
    [data-testid="stSelectbox"]>div>div{background-color:#E8F4FD!important;border:1.5px solid #2C7BE5!important;border-radius:8px!important;color:#0F2B17!important}
    [data-testid="stFileUploader"]>div{background-color:#F0FAF0!important;border:2px dashed #2F9E44!important;border-radius:10px!important}
    div[data-testid="stSidebar"] div.stButton>button{background:#E03131!important;color:white!important;border:none!important;border-radius:10px!important;font-weight:700!important;width:100%!important;padding:.6rem!important}
    div[data-testid="stSidebar"] div.stButton>button:hover{background:#C92A2A!important}
    </style>""", unsafe_allow_html=True)


# ─────────────────────────────────────────────
# LOGIN
# ─────────────────────────────────────────────

def render_login_page():
    st.markdown("""<style>
    [data-testid="stSidebar"]{display:none!important}
    [data-testid="stHeader"]{display:none!important}
    .stApp{background:linear-gradient(135deg,#0A1F10 0%,#123B1C 55%,#1A5C2A 100%)!important}
    </style>""", unsafe_allow_html=True)
    _, col, _ = st.columns([1, 1.1, 1])
    with col:
        st.markdown("<div style='height:6vh'></div>", unsafe_allow_html=True)
        st.markdown("""<div class="login-card"><div class="login-header">
            <div class="login-icon-ring">🗄️</div>
            <div class="login-app-name">CoopArchive</div>
            <div class="login-org">SOCOOPACDI</div>
            <div class="login-tagline">Système de gestion et d'archivage de documents</div>
        </div><div class="login-divider"></div></div>""", unsafe_allow_html=True)
        st.markdown('<div class="login-card" style="margin-top:-1rem;border-top-left-radius:0;border-top-right-radius:0;padding-top:0;">', unsafe_allow_html=True)
        email    = st.text_input("📧 Adresse email", placeholder="exemple@socoopacdi.local", key="login_email")
        password = st.text_input("🔒 Mot de passe", type="password", placeholder="••••••••", key="login_password")
        st.markdown("<div style='height:.5rem'></div>", unsafe_allow_html=True)
        if st.button("🔐  Se connecter", use_container_width=True, type="primary", key="login_btn"):
            if not email.strip() or not password.strip():
                st.error("Veuillez renseigner l'email et le mot de passe.")
            else:
                user = authenticate_user(email, password)
                if user:
                    st.session_state["current_user"] = user
                    st.session_state["page"] = "Tableau de bord"
                    insert_activity(user["id"], "Connexion", user["name"], None, "Connexion réussie.")
                    st.rerun()
                else:
                    st.error("❌ Identifiants incorrects ou compte inactif.")
        st.markdown('<div class="login-footer">© 2026 SOCOOPACDI · CoopArchive v3.0</div>', unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)


# ─────────────────────────────────────────────
# SIDEBAR & TOPBAR
# ─────────────────────────────────────────────

def render_sidebar() -> str:
    with st.sidebar:
        st.markdown('<div class="brand-box"><div class="brand-title">🗄️ CoopArchive</div><div class="brand-subtitle">Archive Documents SOCOOPACDI</div></div>', unsafe_allow_html=True)
        pages = [
            "Tableau de bord", "Documents", "Ajouter document",
            "Catégories", "Utilisateurs", "Rapports & Statistiques",
            "Corbeille", "Journal des activités", "Paramètres",
        ]
        if "page" not in st.session_state:
            st.session_state["page"] = "Tableau de bord"
        current_index = pages.index(st.session_state["page"]) if st.session_state["page"] in pages else 0
        page = st.radio("Navigation", pages, index=current_index, label_visibility="collapsed", key="nav_radio")
        st.session_state["page"] = page
        st.markdown("---")
        current_user = st.session_state.get("current_user")
        if current_user:
            st.markdown(f'<div class="brand-box"><div class="brand-title">{current_user["name"]}</div><div class="brand-subtitle">● {current_user["role"]}</div></div>', unsafe_allow_html=True)
            if st.button("🚪 Se déconnecter", use_container_width=True, key="logout_btn"):
                st.session_state.clear()
                st.rerun()
    return st.session_state["page"]


def render_topbar(page: str) -> str:
    left, right = st.columns([4, 1])
    with left:
        st.markdown(f'<div class="app-title">{page}</div>', unsafe_allow_html=True)
        st.markdown('<div class="app-subtitle">Application locale d\'archivage — SOCOOPACDI</div>', unsafe_allow_html=True)
    with right:
        docs_df  = get_documents_df()
        expiring = expiring_documents_count(docs_df)
        st.info(f"⚠️ {expiring} doc(s) expirent bientôt" if expiring else "✅ Aucune alerte")
    return st.text_input("Recherche globale", placeholder="🔍  Rechercher un document...", label_visibility="collapsed")


def filter_documents(df: pd.DataFrame, query: str) -> pd.DataFrame:
    if df.empty or not query.strip():
        return df
    q    = query.lower()
    mask = (
        df["title"].str.lower().str.contains(q, na=False)
        | df["category_name"].str.lower().str.contains(q, na=False)
        | df["owner_name"].str.lower().str.contains(q, na=False)
        | df["description"].str.lower().str.contains(q, na=False)
        | df["tags"].str.lower().str.contains(q, na=False)
    )
    return df[mask].copy()


# ─────────────────────────────────────────────
# APERÇU DOCUMENT
# ─────────────────────────────────────────────

def render_document_preview(row: pd.Series) -> None:
    ext = str(row.get("extension", "")).lower()
    st.markdown('<div class="preview-box">', unsafe_allow_html=True)
    st.markdown(f"**Aperçu : {row['title']}**")
    file_bytes = download_document_bytes(row["stored_filename"], row.get("storage_path", ""))
    if file_bytes is None:
        st.caption("⚠️ Fichier introuvable dans le stockage cloud.")
    elif ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
        st.image(file_bytes, use_container_width=True)
    elif ext == ".pdf":
        import base64
        b64 = base64.b64encode(file_bytes).decode("utf-8")
        st.markdown(f'<iframe src="data:application/pdf;base64,{b64}" width="100%" height="500px" style="border:none;border-radius:8px;"></iframe>', unsafe_allow_html=True)
    elif ext in {".txt", ".md", ".csv", ".log", ".json", ".xml", ".html", ".py", ".js"}:
        try:
            content = file_bytes.decode("utf-8", errors="replace")
            lines   = content.splitlines()
            preview = "\n".join(lines[:100])
            if len(lines) > 100:
                preview += f"\n\n… ({len(lines) - 100} lignes supplémentaires)"
            if ext == ".csv":
                try:
                    st.dataframe(pd.read_csv(io.StringIO(content), nrows=20), use_container_width=True)
                except Exception:
                    st.code(preview, language="text")
            else:
                lang_map = {".py": "python", ".js": "javascript", ".json": "json", ".xml": "xml", ".html": "html", ".md": "markdown"}
                st.code(preview, language=lang_map.get(ext, "text"))
        except Exception as e:
            st.caption(f"Impossible de lire le fichier : {e}")
    elif ext in {".xlsx", ".xls"}:
        try:
            st.dataframe(pd.read_excel(io.BytesIO(file_bytes), nrows=20), use_container_width=True)
        except Exception as e:
            st.caption(f"Impossible de prévisualiser ce fichier Excel : {e}")
    else:
        st.caption(f"Aperçu non disponible pour les fichiers `{ext}`.")
    st.markdown("</div>", unsafe_allow_html=True)


# ─────────────────────────────────────────────
# PAGES
# ─────────────────────────────────────────────

def render_dashboard(search_query: str):
    documents_df  = filter_documents(get_documents_df(), search_query)
    activities_df = get_recent_activities_df(8)
    metrics       = dashboard_metrics(documents_df)

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.markdown(card_html("Total documents",    f"{metrics['total_documents']:,}".replace(",", ""), "Tous les documents actifs", "#2C7BE5", "📄"), unsafe_allow_html=True)
    c2.markdown(card_html("Ajoutés aujourd'hui", str(metrics["added_today"]),  "Nouveaux ce jour",       "#2F9E44", "☁️"),  unsafe_allow_html=True)
    c3.markdown(card_html("Catégories",          str(metrics["categories"]),   "Types de documents",     "#7B2CBF", "🗃️"), unsafe_allow_html=True)
    c4.markdown(card_html("Utilisateurs",        str(metrics["users_total"]),  "Comptes actifs",         "#F08C00", "👥"), unsafe_allow_html=True)
    c5.markdown(card_html("Stockage utilisé",    metrics["used_storage"],      "Sur 15 GB Google Drive", "#1098AD", "💾"), unsafe_allow_html=True)

    left, center, right = st.columns([1.05, 1.45, 1.15])
    with left:
        st.markdown('<div class="section-card">', unsafe_allow_html=True)
        st.markdown('<div class="section-title">Documents par catégorie</div>', unsafe_allow_html=True)
        if documents_df.empty:
            st.info("Aucun document disponible.")
        else:
            cc = documents_df.groupby("category_name", as_index=False)["id"].count().rename(columns={"id": "total"})
            cc["color"] = cc["category_name"].map(lambda n: CATEGORY_STYLES.get(n, {}).get("color", "#0EA5E9"))
            fig = px.pie(cc, names="category_name", values="total", hole=0.52, color="category_name",
                         color_discrete_map={r["category_name"]: r["color"] for _, r in cc.iterrows()})
            fig.update_layout(height=320, margin=dict(l=0, r=0, t=10, b=10), showlegend=False)
            st.plotly_chart(fig, use_container_width=True)
            for _, row in cc.sort_values("total", ascending=False).iterrows():
                st.markdown(f"<div class='doc-row'><span class='small-muted'>{row['category_name']}</span><span style='float:right;font-weight:700'>{int(row['total'])}</span></div>", unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)

    with center:
        st.markdown('<div class="section-card">', unsafe_allow_html=True)
        st.markdown('<div class="section-title">Documents ajoutés par mois</div>', unsafe_allow_html=True)
        if documents_df.empty:
            st.info("Ajoutez des documents pour alimenter le graphique.")
        else:
            monthly = documents_df.groupby(documents_df["created_at_dt"].dt.to_period("M"))["id"].count().reset_index()
            monthly.columns = ["period", "count"]
            monthly["month"]     = monthly["period"].astype(str)
            monthly["cum_total"] = monthly["count"].cumsum()
            fig = px.line(monthly, x="month", y="cum_total", markers=True, labels={"month": "Mois", "cum_total": "Documents cumulés"})
            fig.update_traces(line_color="#2C7BE5", line_width=2.5)
            fig.update_layout(height=320, margin=dict(l=0, r=0, t=10, b=10), xaxis_title="", yaxis_title="")
            st.plotly_chart(fig, use_container_width=True)
        st.markdown("</div>", unsafe_allow_html=True)

    with right:
        st.markdown('<div class="section-card">', unsafe_allow_html=True)
        st.markdown('<div class="section-title">Documents récents</div>', unsafe_allow_html=True)
        if documents_df.empty:
            st.info("Aucun document trouvé.")
        else:
            for _, row in documents_df.head(6).iterrows():
                st.markdown(f"""<div class="doc-row"><div style="display:flex;gap:.8rem;align-items:flex-start;">
                    <div style="width:14px;height:14px;border-radius:4px;background:{row['category_color']};margin-top:.25rem;flex-shrink:0;"></div>
                    <div style="flex:1;min-width:0;"><div style="font-weight:700;color:#1E3122;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{row['title']}</div>
                    <div class="small-muted">{row['category_name']} · {row['owner_name']}</div></div>
                    <div class="small-muted" style="flex-shrink:0;">{pd.to_datetime(row['created_at']).strftime('%d/%m')}</div>
                </div></div>""", unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)

    bl, br = st.columns([1.4, 1.0])
    with bl:
        st.markdown('<div class="section-card">', unsafe_allow_html=True)
        st.markdown('<div class="section-title">Activités récentes</div>', unsafe_allow_html=True)
        if activities_df.empty:
            st.info("Aucune activité enregistrée.")
        else:
            for _, row in activities_df.iterrows():
                color = ACTION_COLORS.get(row["action"], "#6C757D")
                tl    = pd.to_datetime(row["created_at"]).strftime("%d/%m %H:%M")
                st.markdown(f"""<div class="activity-row"><div style="display:flex;gap:.8rem;align-items:center;">
                    <div>👤</div><div style="flex:1;"><strong>{row['user_name']}</strong> — <em>{row['label']}</em></div>
                    <span class="pill" style="background:{color};">{row['action']}</span>
                    <div class="small-muted">{tl}</div>
                </div></div>""", unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)

    with br:
        st.markdown('<div class="section-card">', unsafe_allow_html=True)
        st.markdown('<div class="section-title">💾 Espace de stockage</div>', unsafe_allow_html=True)
        used, capacity = storage_stats(documents_df)
        ratio = min((used / capacity) * 100, 100) if capacity else 0
        st.markdown(f'<div style="margin-bottom:.5rem;"><span style="font-size:.85rem;color:#6C757D;">Documents archivés</span><br><span style="font-weight:700;font-size:1.1rem;color:#2C7BE5;">{format_size(used)}</span><span style="color:#6C757D;font-size:.85rem;"> / {format_size(capacity)}</span></div>', unsafe_allow_html=True)
        st.progress(ratio / 100)
        st.markdown(f'<div style="display:flex;justify-content:space-between;font-size:.82rem;margin-top:.2rem;"><span style="color:#6C757D;">Google Drive (15 GB)</span><span style="font-weight:700;color:#2C7BE5;">{ratio:.1f}%</span></div>', unsafe_allow_html=True)
        st.markdown('<div class="section-title" style="margin-top:1rem;">🔔 Alertes</div>', unsafe_allow_html=True)
        count_exp = expiring_documents_count(documents_df)
        if count_exp:
            st.warning(f"⚠️ {count_exp} document(s) expirent dans 30 jours.")
        else:
            st.success("✅ Aucune alerte d'expiration imminente.")
        st.markdown("</div>", unsafe_allow_html=True)


def render_documents_page(search_query: str):
    st.subheader("📁 Documents")
    df = filter_documents(get_documents_df(), search_query)
    if df.empty:
        st.info("Aucun document trouvé.")
        return
    c1, c2, c3 = st.columns(3)
    with c1: sel_cat    = st.selectbox("Catégorie",    ["Toutes"] + sorted(df["category_name"].dropna().unique().tolist()))
    with c2: sel_owner  = st.selectbox("Propriétaire", ["Tous"]   + sorted(df["owner_name"].dropna().unique().tolist()))
    with c3: sel_status = st.selectbox("Statut",       ["Tous", "Actif", "Archivé"])
    filtered = df.copy()
    if sel_cat    != "Toutes": filtered = filtered[filtered["category_name"] == sel_cat]
    if sel_owner  != "Tous":   filtered = filtered[filtered["owner_name"]    == sel_owner]
    if sel_status != "Tous":   filtered = filtered[filtered["status"]        == sel_status]
    st.markdown(f"**{len(filtered)} document(s) trouvé(s)**")
    st.dataframe(
        filtered[["id", "title", "category_name", "owner_name", "size_bytes", "created_at", "expires_at", "status"]]
        .rename(columns={"id": "ID", "title": "Titre", "category_name": "Catégorie", "owner_name": "Propriétaire",
                         "size_bytes": "Taille", "created_at": "Créé le", "expires_at": "Expiration", "status": "Statut"})
        .assign(
            Taille=lambda x: filtered["size_bytes"].map(format_size).values,
            **{"Créé le": lambda x: pd.to_datetime(filtered["created_at"]).dt.strftime("%d/%m/%Y %H:%M").values},
            Expiration=lambda x: pd.to_datetime(filtered["expires_at"], errors="coerce").dt.strftime("%d/%m/%Y").fillna("—").values,
        ),
        use_container_width=True, hide_index=True,
    )

    st.markdown("### Actions par document")
    uid = get_current_user_id()
    for _, row in filtered.iterrows():
        doc_id = int(row["id"])
        with st.expander(f"{row['category_icon']}  {row['title']}  ·  {row['category_name']}  ·  {format_size(int(row['size_bytes']))}", expanded=False):
            info_col, action_col = st.columns([2.2, 1])
            with info_col:
                st.write(f"**Propriétaire :** {row['owner_name']}")
                st.write(f"**Créé le :** {format_datetime(row['created_at'])}")
                st.write(f"**Expiration :** {format_date(row.get('expires_at'))}")
                st.write(f"**Description :** {row.get('description') or '—'}")
                st.write(f"**Tags :** {row.get('tags') or '—'}")
                st.write(f"**Statut :** {row.get('status')}")
            with action_col:
                file_bytes = download_document_bytes(row["stored_filename"], row.get("storage_path", ""))
                if file_bytes:
                    st.download_button("⬇️ Télécharger", data=file_bytes, file_name=row["original_filename"],
                                       mime=row.get("mime_type") or "application/octet-stream", key=f"dl_{doc_id}")
                else:
                    st.caption("Fichier introuvable.")
                if st.button("🗑️ Mettre à la corbeille", key=f"del_{doc_id}", use_container_width=True):
                    soft_delete_document(doc_id, uid)
                    st.success(f"« {row['title']} » déplacé dans la corbeille.")
                    st.rerun()
            st.markdown("---")
            edit_key = f"show_edit_{doc_id}"
            if edit_key not in st.session_state:
                st.session_state[edit_key] = False
            col_edit, col_prev, _ = st.columns([1, 1, 2])
            with col_edit:
                if st.button("🔒 Fermer" if st.session_state[edit_key] else "✏️ Modifier", key=f"tog_edit_{doc_id}", use_container_width=True):
                    st.session_state[edit_key] = not st.session_state[edit_key]
                    st.rerun()
            with col_prev:
                pk = f"show_preview_{doc_id}"
                if pk not in st.session_state:
                    st.session_state[pk] = False
                if st.button("🙈 Masquer aperçu" if st.session_state[pk] else "👁️ Aperçu", key=f"tog_prev_{doc_id}", use_container_width=True):
                    st.session_state[pk] = not st.session_state[pk]
                    st.rerun()
            if st.session_state[edit_key]:
                current_expiry = None
                ev = row.get("expires_at", "")
                if ev and str(ev) not in ("", "None", "—"):
                    try:
                        current_expiry = datetime.strptime(str(ev)[:10], "%Y-%m-%d").date()
                    except Exception:
                        pass
                with st.form(key=f"edit_form_{doc_id}"):
                    new_title  = st.text_input("Titre", value=row["title"])
                    new_desc   = st.text_area("Description", value=row.get("description") or "")
                    new_tags   = st.text_input("Tags", value=row.get("tags") or "")
                    exp_choice = st.radio("Expiration ?", ["Oui, définir une date", "Non"],
                                         index=0 if current_expiry else 1, key=f"exp_r_{doc_id}", horizontal=True)
                    new_expiry = st.date_input("Date", value=current_expiry or date.today(), format="DD/MM/YYYY",
                                               key=f"exp_d_{doc_id}", disabled=(exp_choice != "Oui, définir une date"))
                    if st.form_submit_button("💾 Enregistrer", use_container_width=True, type="primary"):
                        update_document(doc_id, new_title, new_desc, new_tags,
                                        new_expiry if exp_choice == "Oui, définir une date" else None, uid)
                        st.session_state[edit_key] = False
                        st.success(f"✅ « {new_title} » mis à jour.")
                        st.rerun()
            if st.session_state.get(f"show_preview_{doc_id}", False):
                render_document_preview(row)


def render_add_document_page():
    st.subheader("➕ Ajouter un document")
    cats_df  = get_categories_df()
    users_df = get_users_df()
    if cats_df.empty or users_df.empty:
        st.error("Ajoutez d'abord au moins une catégorie et un utilisateur.")
        return
    cat_opts  = {f"{r['icon']} {r['name']}": int(r["id"]) for _, r in cats_df.iterrows()}
    user_opts = {f"{r['name']} · {r['role']}": int(r["id"]) for _, r in users_df.iterrows()}
    with st.form("add_doc_form", clear_on_submit=True):
        uploaded = st.file_uploader("Fichier à archiver", type=None)
        c1, c2 = st.columns(2)
        with c1:
            title   = st.text_input("Titre du document", placeholder="Laisser vide pour utiliser le nom du fichier")
            cat_lbl = st.selectbox("Catégorie", list(cat_opts.keys()))
        with c2:
            usr_lbl  = st.selectbox("Utilisateur propriétaire", list(user_opts.keys()))
            has_exp  = st.checkbox("Ce document a une date d'expiration", value=False)
            expires  = st.date_input("Date d'expiration", value=date.today(), disabled=not has_exp)
        description = st.text_area("Description (optionnelle)")
        tags        = st.text_input("Tags", placeholder="ex: contrat, fournisseur, 2026")
        submitted   = st.form_submit_button("💾 Enregistrer le document")
    if submitted:
        if uploaded is None:
            st.error("Veuillez sélectionner un fichier.")
            return
        with st.spinner("Envoi vers Google Drive..."):
            add_document(title or uploaded.name, uploaded, cat_opts[cat_lbl], user_opts[usr_lbl],
                         description, tags, expires if has_exp else None)
        st.success(f"✅ Document « {title or uploaded.name} » enregistré avec succès.")
        st.rerun()


def render_categories_page():
    st.subheader("🗃️ Catégories")
    df = get_categories_df()
    if not df.empty:
        st.dataframe(
            df[["id", "name", "icon", "color", "created_at"]].rename(
                columns={"id": "ID", "name": "Nom", "icon": "Icône", "color": "Couleur", "created_at": "Créée le"}
            ),
            use_container_width=True, hide_index=True,
        )
    else:
        st.info("Aucune catégorie définie.")
    st.markdown("### Ajouter une catégorie")
    with st.form("add_cat_form", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        with c1: name  = st.text_input("Nom de la catégorie")
        with c2: color = st.color_picker("Couleur", "#2C7BE5")
        with c3: icon  = st.text_input("Icône (emoji)", value="🗂️")
        if st.form_submit_button("➕ Ajouter la catégorie"):
            if not name.strip():
                st.error("Le nom est obligatoire.")
            else:
                add_category(name, color, icon)
                st.success(f"✅ Catégorie « {name} » ajoutée.")
                st.rerun()


def render_users_page():
    st.subheader("👥 Utilisateurs")
    current_user = st.session_state.get("current_user", {})
    df = get_users_df()
    if df.empty:
        st.info("Aucun utilisateur.")
    else:
        st.markdown(f"**{len(df)} utilisateur(s) enregistré(s)**")
        for _, row in df.iterrows():
            role_colors = {"Administrateur": "#E03131", "Archiviste": "#2C7BE5", "Consultant": "#F08C00", "Visiteur": "#6C757D"}
            is_current  = current_user.get("id") == int(row["id"])
            with st.expander(f"👤  {row['name']}  ·  {row['role']}  {'(vous)' if is_current else ''}", expanded=False):
                c1, c2, c3 = st.columns([2, 2, 1])
                with c1:
                    st.markdown(f"**Nom :** {row['name']}")
                    st.markdown(f"**Email :** {row['email']}")
                with c2:
                    rc = role_colors.get(str(row["role"]), "#6C757D")
                    st.markdown(f"**Rôle :** <span style='background:{rc};color:white;padding:2px 10px;border-radius:6px;font-size:.85rem;'>{row['role']}</span>", unsafe_allow_html=True)
                    active = row.get("active", False)
                    st.markdown(f"**Statut :** {'🟢 Actif' if active else '🔴 Inactif'}")
                with c3:
                    if is_current:
                        st.caption("Impossible de se supprimer soi-même.")
                    else:
                        ck = f"confirm_del_user_{row['id']}"
                        if ck not in st.session_state:
                            st.session_state[ck] = False
                        if not st.session_state[ck]:
                            if st.button("🗑️ Supprimer", key=f"del_u_{row['id']}", use_container_width=True):
                                st.session_state[ck] = True
                                st.rerun()
                        else:
                            st.warning(f"Confirmer la suppression de **{row['name']}** ?")
                            cy, cn = st.columns(2)
                            with cy:
                                if st.button("✅ Oui", key=f"cy_{row['id']}", use_container_width=True):
                                    try:
                                        delete_user(int(row["id"]))
                                        st.session_state[ck] = False
                                        st.success("Supprimé.")
                                        st.rerun()
                                    except ValueError as e:
                                        st.error(str(e))
                                        st.session_state[ck] = False
                            with cn:
                                if st.button("❌ Non", key=f"cn_{row['id']}", use_container_width=True):
                                    st.session_state[ck] = False
                                    st.rerun()
    st.markdown("---")
    st.markdown("### ➕ Ajouter un utilisateur")
    with st.form("add_user_form", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        with c1: name  = st.text_input("Nom complet")
        with c2: email = st.text_input("Adresse email")
        with c3: role  = st.selectbox("Rôle", ["Administrateur", "Archiviste", "Consultant", "Visiteur"])
        c4, _ = st.columns(2)
        with c4: pwd = st.text_input("Mot de passe", type="password", placeholder="Minimum 6 caractères")
        if st.form_submit_button("➕ Ajouter l'utilisateur"):
            if not name.strip() or not email.strip():
                st.error("Nom et email sont obligatoires.")
            elif len(pwd) < 6:
                st.error("Le mot de passe doit comporter au moins 6 caractères.")
            else:
                add_user(name, email, role, pwd)
                st.success(f"✅ Utilisateur « {name} » ajouté.")
                st.rerun()


def render_reports_page(search_query: str):
    st.subheader("📈 Rapports & Statistiques")
    df = filter_documents(get_documents_df(), search_query)
    if df.empty:
        st.info("Aucune donnée disponible.")
        return
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Total documents",    len(df))
    k2.metric("Taille totale",      format_size(int(df["size_bytes"].sum())))
    k3.metric("Expirant (30j)",     expiring_documents_count(df))
    k4.metric("Catégories actives", df["category_name"].nunique())
    st.markdown("---")
    c1, c2 = st.columns(2)
    with c1:
        cc = df.groupby("category_name", as_index=False)["id"].count().rename(columns={"id": "documents"})
        cm = {r["category_name"]: CATEGORY_STYLES.get(r["category_name"], {}).get("color", "#0EA5E9") for _, r in cc.iterrows()}
        fig = px.bar(cc, x="category_name", y="documents", color="category_name", color_discrete_map=cm, title="Répartition par catégorie")
        fig.update_layout(showlegend=False)
        st.plotly_chart(fig, use_container_width=True)
    with c2:
        oc  = df.groupby("owner_name", as_index=False)["id"].count().rename(columns={"id": "documents"})
        fig = px.bar(oc, x="owner_name", y="documents", title="Documents par utilisateur")
        st.plotly_chart(fig, use_container_width=True)
    monthly = df.groupby(df["created_at_dt"].dt.to_period("M"))["id"].count().reset_index()
    monthly.columns = ["period", "count"]
    monthly["mois"] = monthly["period"].astype(str)
    fig = px.line(monthly, x="mois", y="count", markers=True, title="Ajouts mensuels")
    fig.update_traces(line_color="#2C7BE5", line_width=2)
    st.plotly_chart(fig, use_container_width=True)
    st.markdown("---")
    if st.button("📄 Générer rapport PDF", type="primary"):
        with st.spinner("Génération du PDF..."):
            try:
                pdf = generate_pdf_report(df)
                st.download_button("⬇️ Télécharger le PDF", data=pdf,
                                   file_name=f"rapport_{date.today().strftime('%Y%m%d')}.pdf",
                                   mime="application/pdf")
                st.success("✅ Rapport PDF prêt.")
            except Exception as e:
                st.error(f"Erreur : {e}")


def render_trash_page():
    st.subheader("🗑️ Corbeille")
    df    = get_documents_df(include_deleted=True)
    trash = df[df["is_deleted"] == True].copy() if not df.empty else pd.DataFrame()
    if trash.empty:
        st.success("✅ La corbeille est vide.")
        return
    st.warning(f"⚠️ {len(trash)} document(s) dans la corbeille.")
    uid = get_current_user_id()
    for _, row in trash.iterrows():
        dl = format_datetime(row.get("deleted_at")) if row.get("deleted_at") else "—"
        with st.expander(f"🗑️  {row['title']}  · supprimé le {dl}"):
            c1, c2 = st.columns(2)
            with c1:
                if st.button("♻️ Restaurer", key=f"restore_{row['id']}"):
                    restore_document(int(row["id"]), uid)
                    st.success(f"✅ « {row['title']} » restauré.")
                    st.rerun()
            with c2:
                if st.button("❌ Supprimer définitivement", key=f"purge_{row['id']}"):
                    permanently_delete_document(int(row["id"]), uid)
                    st.success("Supprimé définitivement.")
                    st.rerun()


def render_activity_page():
    st.subheader("📋 Journal des activités")
    acts  = db_read("activities")
    users = db_read("users")
    if not acts:
        st.info("Aucune activité enregistrée.")
        return
    df = pd.DataFrame(acts)
    if users:
        udf = pd.DataFrame(users).rename(columns={"id": "user_id", "name": "user_name"})
        df["user_id"]    = pd.to_numeric(df.get("user_id", pd.Series()), errors="coerce")
        udf["user_id"]   = pd.to_numeric(udf["user_id"], errors="coerce")
        df = df.merge(udf[["user_id", "user_name"]], on="user_id", how="left")
        df["user_name"]  = df["user_name"].fillna("Système")
    else:
        df["user_name"] = "Système"
    df = df.sort_values("created_at", ascending=False).reset_index(drop=True)
    display = df[["id", "user_name", "action", "label", "details", "created_at"]].rename(
        columns={"id": "ID", "user_name": "Utilisateur", "action": "Action",
                 "label": "Document", "details": "Détails", "created_at": "Date"})
    display["Date"] = pd.to_datetime(display["Date"], errors="coerce").dt.strftime("%d/%m/%Y %H:%M").fillna("—")
    st.markdown(f"**{len(display)} entrée(s) dans le journal**")
    st.dataframe(display, use_container_width=True, hide_index=True)


def render_settings_page():
    st.subheader("⚙️ Paramètres de l'application")
    docs_df = get_documents_df(include_deleted=True)
    active  = docs_df[docs_df["is_deleted"] == False] if not docs_df.empty else pd.DataFrame()
    used, capacity = storage_stats(active)
    st.markdown("### Informations système")
    c1, c2, c3 = st.columns(3)
    c1.metric("Base de données",   "Google Drive (JSON)")
    c2.metric("Stockage fichiers", "Google Drive")
    c3.metric("Capacité",          format_size(capacity))
    c4, c5 = st.columns(2)
    c4.metric("Stockage utilisé", format_size(used))
    c5.metric("Documents (total avec corbeille)", len(docs_df) if not docs_df.empty else 0)
    st.markdown("---")
    st.markdown("### Catégories disponibles")
    cats_df = get_categories_df()
    if not cats_df.empty:
        for _, row in cats_df.iterrows():
            st.markdown(f"<span style='background:{row['color']};color:white;padding:3px 10px;border-radius:8px;margin-right:6px;font-size:.9rem;'>{row['icon']} {row['name']}</span>", unsafe_allow_html=True)


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    inject_css()
    initialize_database()

    if "current_user" not in st.session_state or not st.session_state["current_user"]:
        render_login_page()
        return

    page         = render_sidebar()
    search_query = render_topbar(page)

    if   page == "Tableau de bord":         render_dashboard(search_query)
    elif page == "Documents":               render_documents_page(search_query)
    elif page == "Ajouter document":        render_add_document_page()
    elif page == "Catégories":              render_categories_page()
    elif page == "Utilisateurs":            render_users_page()
    elif page == "Rapports & Statistiques": render_reports_page(search_query)
    elif page == "Corbeille":               render_trash_page()
    elif page == "Journal des activités":   render_activity_page()
    elif page == "Paramètres":              render_settings_page()


if __name__ == "__main__":
    main()
