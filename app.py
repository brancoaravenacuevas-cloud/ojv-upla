import os
import sqlite3
import smtplib
import dropbox
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, render_template, send_file, abort
from werkzeug.utils import secure_filename

BASE = Path(__file__).resolve().parent
UPLOADS = BASE / 'uploads'
UPLOADS.mkdir(exist_ok=True)
DB = BASE / 'ojv.sqlite3'

app = Flask(__name__, template_folder=str(BASE / 'templates'))
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024

WRITE_PASSWORD = os.getenv('WRITE_PASSWORD', 'CasoUpla001002')
TRIBUNAL_PASSWORD = os.getenv('TRIBUNAL_PASSWORD', 'TribunalUpla090817')
COURT_EMAIL = os.getenv('COURT_EMAIL', 'jl.playaancha@gmail.com')
SMTP_USER = os.getenv('SMTP_USER', '')
SMTP_APP_PASSWORD = os.getenv('SMTP_APP_PASSWORD', '')
SMTP_HOST = os.getenv('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT = int(os.getenv('SMTP_PORT', '465'))
DROPBOX_ACCESS_TOKEN = os.getenv('DROPBOX_ACCESS_TOKEN')
dbx = dropbox.Dropbox(DROPBOX_ACCESS_TOKEN)

CASES = {
    '1': {
        'rit': 'C-1842-2026',
        'caratula': 'Metalúrgica del Pacífico SpA / Envases del Sur Ltda.',
        'template': 'caso1.html'
    },
    '2': {
        'rit': 'C-1967-2026',
        'caratula': 'Frío Industrial Austral SpA / Alimentos del Litoral Ltda.',
        'template': 'caso2.html'
    }
}


def db():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    con.execute('''CREATE TABLE IF NOT EXISTS records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        case_id TEXT NOT NULL,
        folio INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        type TEXT NOT NULL,
        party TEXT NOT NULL,
        title TEXT NOT NULL,
        description TEXT,
        filename TEXT,
        stored_name TEXT
    )''')
    con.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_case_folio ON records(case_id, folio)')
    con.commit()
    con.close()


def case_ok(case_id):
    return case_id in CASES


def records_for(case_id):
    con = db()
    rows = con.execute('SELECT * FROM records WHERE case_id=? ORDER BY folio', (case_id,)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def send_notification(case_id, record):
    if not SMTP_USER or not SMTP_APP_PASSWORD:
        return False, 'SMTP no configurado'
    c = CASES[case_id]
    try:
        dt = datetime.fromisoformat(record['created_at'].replace('Z', '+00:00'))
        date_text = dt.astimezone().strftime('%d/%m/%Y')
    except Exception:
        date_text = record['created_at']
    msg = EmailMessage()
    msg['Subject'] = f"Nueva actuación ingresada — RIT {c['rit']}"
    msg['From'] = SMTP_USER
    msg['To'] = COURT_EMAIL
    msg.set_content(
        'Hola:\n\n'
        f"{record['party']} presentó un escrito con fecha {date_text}.\n\n"
        f"Folio: {record['folio']}\n"
        f"Expediente: {c['rit']}\n"
        f"Documento: {record['title']}\n"
    )
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.login(SMTP_USER, SMTP_APP_PASSWORD)
        smtp.send_message(msg)
    return True, None


@app.get('/')
def home():
    return render_template('inicio.html')


@app.get('/caso/<case_id>')
def case_page(case_id):
    if not case_ok(case_id):
        abort(404)
    return render_template(CASES[case_id]['template'])


@app.get('/api/casos/<case_id>/actuaciones')
def list_records(case_id):
    if not case_ok(case_id):
        return jsonify(error='Caso no encontrado'), 404
    return jsonify(actuaciones=records_for(case_id))


@app.post('/api/casos/<case_id>/actuaciones')
def create_record(case_id):
    if not case_ok(case_id):
        return jsonify(error='Caso no encontrado'), 404
    if request.form.get('password') != WRITE_PASSWORD:
        return jsonify(error='Contraseña incorrecta. La actuación no fue ingresada.'), 403

    title = (request.form.get('title') or '').strip()
    if not title:
        return jsonify(error='Debes ingresar el nombre o título del documento.'), 400
    type_ = (request.form.get('type') or 'Escrito').strip()
    party = (request.form.get('party') or 'Otro').strip()
    desc = (request.form.get('desc') or '').strip()
    now = datetime.now().astimezone().isoformat(timespec='seconds')

    file = request.files.get('file')
    filename = secure_filename(file.filename) if file and file.filename else None

    con = db()
    try:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT COALESCE(MAX(folio),0)+1 AS next_folio FROM records WHERE case_id=?', (case_id,)).fetchone()
        folio = int(row['next_folio'])
        stored_name = None
                if file and filename:
            stored_name = f"{case_id}_{folio}_{os.urandom(8).hex()}_{filename}"
                with file.stream as f:
                dbx.files_upload(
                    f.read(),
                    f'/OJV UPLA/{stored_name}',
                    mode=dropbox.files.WriteMode.overwrite
                )
        cur = con.execute('''INSERT INTO records
            (case_id, folio, created_at, type, party, title, description, filename, stored_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (case_id, folio, now, type_, party, title, desc, filename, stored_name))
        record_id = cur.lastrowid
        con.commit()
    except Exception:
        con.rollback()
        if stored_name:
            (UPLOADS / stored_name).unlink(missing_ok=True)
        raise
    finally:
        con.close()

    record = {'id': record_id, 'case_id': case_id, 'folio': folio, 'created_at': now,
              'type': type_, 'party': party, 'title': title, 'description': desc,
              'filename': filename, 'stored_name': stored_name}
    email_ok = True
    email_error = None
    try:
        email_ok, email_error = send_notification(case_id, record)
    except Exception as exc:
        email_ok, email_error = False, str(exc)
    return jsonify(ok=True, record=record, email_sent=email_ok, email_error=email_error)


@app.get('/api/actuaciones/<int:record_id>/archivo')
def record_file(record_id):
    con = db()
    row = con.execute('SELECT * FROM records WHERE id=?', (record_id,)).fetchone()
    con.close()
    if not row or not row['stored_name']:
        abort(404)
dropbox_path = f"/OJV UPLA/{row['stored_name']}"

try:
    metadata, response = dbx.files_download(dropbox_path)
    from io import BytesIO
    return send_file(
        BytesIO(response.content),
        download_name=f"Folio_{int(row['folio']):03d}_{row['filename'] or 'documento'}",
        as_attachment=False
    )
except Exception:
    abort(404)


@app.delete('/api/casos/<case_id>/actuaciones/<int:record_id>')
def delete_record(case_id, record_id):
    if not case_ok(case_id):
        return jsonify(error='Caso no encontrado'), 404
    if request.form.get('password') != TRIBUNAL_PASSWORD:
        return jsonify(error='Contraseña incorrecta. La actuación no fue eliminada.'), 403
    con = db()
    try:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT * FROM records WHERE id=? AND case_id=?', (record_id, case_id)).fetchone()
        if not row:
            con.rollback()
            return jsonify(error='Actuación no encontrada'), 404
        con.execute('DELETE FROM records WHERE id=?', (record_id,))
        # Mantener la misma condición de la versión local: reconstruir la foliación.
        rows = con.execute('SELECT id FROM records WHERE case_id=? ORDER BY folio, id', (case_id,)).fetchall()
        for i, r in enumerate(rows, start=1):
            con.execute('UPDATE records SET folio=? WHERE id=?', (i, r['id']))
        con.commit()
        if row['stored_name']:
            (UPLOADS / row['stored_name']).unlink(missing_ok=True)
    finally:
        con.close()
    return jsonify(ok=True)


@app.post('/api/casos/<case_id>/borrar')
def clear_case(case_id):
    if not case_ok(case_id):
        return jsonify(error='Caso no encontrado'), 404
    if request.form.get('password') != TRIBUNAL_PASSWORD:
        return jsonify(error='Contraseña incorrecta. El expediente no fue borrado.'), 403
    con = db()
    rows = con.execute('SELECT stored_name FROM records WHERE case_id=?', (case_id,)).fetchall()
    con.execute('DELETE FROM records WHERE case_id=?', (case_id,))
    con.commit()
    con.close()
    for r in rows:
        if r['stored_name']:
            (UPLOADS / r['stored_name']).unlink(missing_ok=True)
    return jsonify(ok=True)


@app.get('/health')
def health():
    return jsonify(ok=True)


init_db()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '5000')), debug=False)
