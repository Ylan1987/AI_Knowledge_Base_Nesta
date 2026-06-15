import logging
import base64
import email
import sqlite3
from agente_compras import AgenteComprasNesta
from zoho_fetcher import ZohoFetcher

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def reprocesar_ticket(ticket_id):
    print(f"\n--- 🚀 REPROCESANDO TICKET {ticket_id} ---")
    agente = AgenteComprasNesta()
    res = agente.procesar_factura(ticket_id)
    print(f"Resultado Ticket {ticket_id}: {res}")

def reprocesar_uid_zoho(uid_str):
    print(f"\n--- 🚀 REPROCESANDO ZOHO UID {uid_str} ---")
    fetcher = ZohoFetcher()
    fetcher.conectar()
    agente = AgenteComprasNesta()
    
    # Borrar de la caché de Zoho para forzar la lectura
    try:
        conn = sqlite3.connect('agent_state.db')
        conn.execute("DELETE FROM processed_uids WHERE uid = ?", (str(uid_str),))
        conn.commit()
        conn.close()
    except Exception as e:
        print("Aviso al borrar caché Zoho:", e)

    _, data = fetcher.mail.uid('fetch', str(uid_str).encode(), '(BODY.PEEK[])')
    if data and data[0]:
        raw_email = data[0][1]
        msg = email.message_from_bytes(raw_email)
        
        email_data = {'uid': str(uid_str), 'subject': msg.get('Subject', ''), 'from': msg.get('From', ''), 'attachments': []}
        
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
            print(f"XML encontrado en UID {uid_str}. Llamando a Agente de Compras...")
            res = agente.procesar_desde_zoho(email_data)
            print(f"Resultado UID {uid_str}: {res}")
        else:
            print(f"El UID {uid_str} no tiene XML de CFE.")

if __name__ == '__main__':
    reprocesar_ticket(74958)
    reprocesar_uid_zoho(23013)
    reprocesar_uid_zoho(23014)