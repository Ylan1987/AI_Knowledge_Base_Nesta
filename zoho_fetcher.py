import imaplib
import email
import logging
import os
import base64
import sqlite3
import re
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

class ZohoFetcher:
    def __init__(self, db_path=os.getenv('AGENT_STATE_DB', 'agent_state.db')):
        self.imap_url = 'imap.zoho.com'
        self.user = os.getenv('ZOHO_USER')
        self.password = os.getenv('ZOHO_PASSWORD')
        self.db_path = db_path
        self.mail = None
        self._init_db()
def _init_db(self):
    with sqlite3.connect(self.db_path) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS processed_emails 
                        (email_uid TEXT PRIMARY KEY, processed_at TIMESTAMP)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS ignored_emails 
                        (email_uid TEXT PRIMARY KEY, ignored_at TIMESTAMP)''')
        conn.commit()

def _is_already_known(self, uid):
    with sqlite3.connect(self.db_path) as conn:
        uid_str = str(uid.decode() if isinstance(uid, bytes) else uid).strip()
        res_p = conn.execute("SELECT 1 FROM processed_emails WHERE email_uid = ?", (uid_str,)).fetchone()
        res_i = conn.execute("SELECT 1 FROM ignored_emails WHERE email_uid = ?", (uid_str,)).fetchone()
        return bool(res_p or res_i)

def _mark_as_ignored(self, uid):
    with sqlite3.connect(self.db_path) as conn:
        uid_str = str(uid.decode() if isinstance(uid, bytes) else uid).strip()
        conn.execute("INSERT OR IGNORE INTO ignored_emails (email_uid, ignored_at) VALUES (?, CURRENT_TIMESTAMP)", (uid_str,))
        conn.commit()

def _mark_as_processed(self, uid):
    with sqlite3.connect(self.db_path) as conn:
        uid_str = str(uid.decode() if isinstance(uid, bytes) else uid).strip()
        conn.execute("INSERT OR IGNORE INTO processed_emails (email_uid, processed_at) VALUES (?, CURRENT_TIMESTAMP)", (uid_str,))
        conn.commit()
    def conectar(self):
        try:
            self.mail = imaplib.IMAP4_SSL(self.imap_url)
            self.mail.login(self.user, self.password)
            self.mail.select('INBOX', readonly=True)
            logging.info(f"✅ Conectado a Zoho en modo LECTURA: {self.user}")
            return True
        except Exception as e:
            logging.exception("Error conectando a Zoho:")
            return False

    def obtener_correos_nuevos(self):
        if not self.mail:
            if not self.conectar(): return []

        hace_3_dias = (datetime.now() - timedelta(days=3)).strftime("%d-%b-%Y")
        search_criteria = f'(SINCE "{hace_3_dias}")'
        
        try:
            _, data = self.mail.uid('search', None, search_criteria)
            uids = data[0].split()
            logging.info(f"🔎 Zoho: {len(uids)} correos encontrados desde {hace_3_dias}.")
        except Exception as e:
            logging.exception("Error en el comando de búsqueda de Zoho:")
            return []

        novedades = []
        for uid in uids:
            uid_str = uid.decode() if isinstance(uid, bytes) else str(uid)
            uid_str = uid_str.strip()
            
            conocido = self._is_already_known(uid_str)
            logging.info(f"🔍 Evaluando UID {uid_str} | Conocido: {conocido}")
            
            if conocido: continue
            
            _, data = self.mail.uid('fetch', uid, '(BODY.PEEK[])')
            if not data or not data[0]: continue
            
            raw_email = data[0][1]
            msg = email.message_from_bytes(raw_email)
            
            remitente = msg.get('From', 'Desconocido')
            asunto = msg.get('Subject', 'Sin Asunto')
            logging.info(f"📧 Escaneando mail: {asunto} de {remitente} (UID: {uid_str})")
            
            email_data = {'uid': uid_str, 'subject': asunto, 'from': remitente, 'attachments': []}
            
            tiene_xml_cfe = False
            for part in msg.walk():
                if part.get_content_maintype() == 'multipart' or part.get('Content-Disposition') is None:
                    continue
                
                filename = part.get_filename()
                if filename:
                    payload = part.get_payload(decode=True)
                    if filename.lower().endswith('.xml') and (b'<CFE ' in payload or b'dgife' in payload):
                        tiene_xml_cfe = True
                        email_data['attachments'].append({'name': filename, 'mimetype': 'application/xml', 'datas': base64.b64encode(payload).decode('utf-8')})
                    elif filename.lower().endswith('.pdf'):
                        email_data['attachments'].append({'name': filename, 'mimetype': 'application/pdf', 'datas': base64.b64encode(payload).decode('utf-8')})
            
            if tiene_xml_cfe:
                logging.info(f"📑 Factura XML detectada en UID {uid_str}. Agregando a la cola.")
                novedades.append(email_data)
                self._mark_as_processed(uid)
            else:
                logging.info(f"⏭️ UID {uid_str} no tiene XML de factura. Ignorando a futuro.")
                self._mark_as_ignored(uid)
        
        return novedades

if __name__ == "__main__":
    fetcher = ZohoFetcher()
    fetcher.obtener_correos_nuevos()
