import os
import json
import logging
import odoorpc
import base64
import re
import sqlite3
import requests
from datetime import datetime, timedelta
from google import genai
from dotenv import load_dotenv
from cfe_expert_parser import CFEExpertParser
from collections import Counter

load_dotenv()

# Parche de Seguridad Docker (QNAP) - Validado 09/06
if not os.getenv('GOOGLE_APPLICATION_CREDENTIALS'):
    os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = '/app/gcloud_credentials.json'

os.environ['GOOGLE_CLOUD_PROJECT'] = 'project-6966617c-3e1f-4ae1-91c'

# Configuración de Logs Verbose
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

DB_NAME = 'agent_state.db'

class AgenteComprasNesta:
    """
    Agente Contador Senior: Automatización total con jerarquía XML > PDF.
    Protocolos de Factura, NC y Cobranzas con Doble Validación de Seguridad.
    """
    def __init__(self):
        self.host = 'prod17.odoo.imprentadiagonal.com.uy'
        self.db = 'odoo17_prod'
        self.user = 'ylan.archimowicz@imprentadiagonal.com.uy'
        self.password = os.getenv('ODOO_PASSWORD', '9a50ca725dd8c0e451e8005982589dcdf3bf8fe7')
        self.odoo = None
        self._context_cache = {} 
        # Configuración Vertex AI Enterprise (Validada 09/06)
        self.client = genai.Client(
            vertexai=True,
            project="project-6966617c-3e1f-4ae1-91c",
            location="global"
        )
        self.model_name = 'gemini-3.5-flash'

    def conectar(self):
        if not self.odoo:
            logging.info("🔌 Conectando a Odoo...")
            self.odoo = odoorpc.ODOO(self.host, protocol='jsonrpc+ssl', port=443, timeout=300)
            self.odoo.login(self.db, self.user, self.password)

    def _get_doc_state(self, rut, tipo_cfe, serie, nro):
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT xml_ok, pdf_proveedor_ok, pdf_ucfe_ok, odoo_move_id FROM cfe_estado WHERE rut=? AND tipo_cfe=? AND serie=? AND nro=?", (rut, str(tipo_cfe), serie, nro))
            row = cursor.fetchone()
            if row: return {'xml_ok': bool(row[0]), 'pdf_proveedor_ok': bool(row[1]), 'pdf_ucfe_ok': bool(row[2]), 'odoo_move_id': row[3]}
            return {'xml_ok': False, 'pdf_proveedor_ok': False, 'pdf_ucfe_ok': False, 'odoo_move_id': None}

    def _update_doc_state(self, rut, tipo_cfe, serie, nro, xml_ok=None, pdf_prov_ok=None, pdf_ucfe_ok=None, move_id=None):
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO cfe_estado (rut, tipo_cfe, serie, nro) VALUES (?, ?, ?, ?)", (rut, str(tipo_cfe), serie, nro))
        updates = []
        params = []
        if xml_ok is not None: updates.append("xml_ok=?"); params.append(int(xml_ok))
        if pdf_prov_ok is not None: updates.append("pdf_proveedor_ok=?"); params.append(int(pdf_prov_ok))
        if pdf_ucfe_ok is not None: updates.append("pdf_ucfe_ok=?"); params.append(int(pdf_ucfe_ok))
        if move_id is not None: updates.append("odoo_move_id=?"); params.append(move_id)
        if updates:
            updates.append("fecha_actualizacion=CURRENT_TIMESTAMP")
            if xml_ok: updates.append("estado='PROCESADO'")
            query = f"UPDATE cfe_estado SET {', '.join(updates)} WHERE rut=? AND tipo_cfe=? AND serie=? AND nro=?"
            params.extend([rut, str(tipo_cfe), serie, nro])
            cursor.execute(query, tuple(params))
        conn.commit()
        conn.close()

    def _limpiar_texto(self, texto):
        if not texto: return ""
        # 1. Quitar HTML
        texto = re.sub(r'<[^>]+>', '', str(texto))
        # 2. Normalizar espacios y saltos de línea
        texto = " ".join(texto.split())
        # 3. Limitar longitud para nombres de productos/cuentas
        return texto[:100]

    def _get_vendor_context(self, rut):
        if not rut: return {"vendedor": None, "historial": [], "es_cliente": False, "journals_preferidos": []}
        if rut in self._context_cache: return self._context_cache[rut]
        logging.info(f"🔍 Cargando historial Odoo para RUT: {rut}...")
        p_ids = self.odoo.env['res.partner'].search([('vat', '=', rut)], limit=1)
        if not p_ids: return {"vendedor": None, "historial": [], "es_cliente": False, "journals_preferidos": []}
        p_id = p_ids[0]
        p_data = self.odoo.env['res.partner'].read([p_id], ['name', 'razon_social', 'fantasy_name', 'customer_rank', 'supplier_rank', 'vat'])[0]
        
        # Optimización Senior: Traemos solo lo necesario para la IA
        bills = self.odoo.env['account.move'].search_read([('partner_id', '=', p_id), ('move_type', '=', 'in_invoice'), ('state', '=', 'posted')], ['invoice_line_ids'], limit=20, order='invoice_date desc')
        
        duplas_vistas = set()
        historial_optimizado = []
        
        all_line_ids = []
        for b in bills:
            if b.get('invoice_line_ids'):
                all_line_ids.extend(b['invoice_line_ids'])
                
        if all_line_ids:
            lines = self.odoo.env['account.move.line'].search_read([('id', 'in', all_line_ids), ('display_type', 'in', ('product', False))], ['name', 'product_id', 'account_id'], limit=100)
            for l in lines:
                p_id_val = l['product_id'][0] if isinstance(l['product_id'], (list, tuple)) else None
                a_id_val = l['account_id'][0] if isinstance(l['account_id'], (list, tuple)) else None
                if not p_id_val or not a_id_val: continue
                
                # Consolidación por descripción + IDs para evitar duplicados infinitos
                clave = f"{self._limpiar_texto(l['name'])}_{p_id_val}_{a_id_val}"
                if clave not in duplas_vistas:
                    historial_optimizado.append({
                        "desc": self._limpiar_texto(l['name']),
                        "p_id": p_id_val,
                        "p_nom": self._limpiar_texto(l['product_id'][1]),
                        "a_id": a_id_val,
                        "a_nom": self._limpiar_texto(l['account_id'][1])
                    })
                    duplas_vistas.add(clave)
                    if len(historial_optimizado) > 40: break # Suficiente "sabiduría"

        all_payments = self.odoo.env['account.payment'].search_read([('partner_id', '=', p_id), ('state', '=', 'posted'), ('payment_type', '=', 'outbound')], ['journal_id'], limit=50)
        journals_hist = []
        for p in all_payments:
            if p.get('journal_id'):
                journals_hist.append({"id": p['journal_id'][0], "name": self._limpiar_texto(p['journal_id'][1])})
        
        unique_journals = {j['id']: j['name'] for j in journals_hist}
        common_journals = [{"id": j_id, "name": name} for j_id, name in unique_journals.items()]
        
        ctx = {
            "vendedor": {k: self._limpiar_texto(v) if isinstance(v, str) else v for k, v in p_data.items()}, 
            "historial": historial_optimizado, 
            "es_cliente": (p_data.get('customer_rank', 0) > 0), 
            "journals_disponibles": common_journals,
            "journals_preferidos": [j['id'] for j in common_journals[:3]]
        }
        self._context_cache[rut] = ctx
        return ctx

    def _get_bill_by_ref_flexible(self, partner_id, full_ref):
        if not full_ref: return None
        only_numbers = "".join(re.findall(r'\d+', str(full_ref))).lstrip('0')
        if not only_numbers: return None
        domain = [('move_type', 'in', ('in_invoice', 'in_refund')), ('ref', '=', full_ref)]
        if partner_id: domain.append(('partner_id', '=', partner_id))
        found = self.odoo.env['account.move'].search(domain)
        if not found:
            domain_flex = [('move_type', 'in', ('in_invoice', 'in_refund')), ('ref', 'ilike', f"%{only_numbers}%")]
            if partner_id: domain_flex.append(('partner_id', '=', partner_id))
            found_flex = self.odoo.env['account.move'].search_read(domain_flex, ['id', 'ref'])
            for b in found_flex:
                if "".join(re.findall(r'\d+', str(b['ref']))).lstrip('0') == only_numbers: return b['id']
        return found[0] if found else None

    def _programar_pago_senior(self, bill_id, partner_id, datos_factura, ctx, ai_datos=None, alertas=None):
        if alertas is None: alertas = []
        ai_datos = ai_datos or {}
        pago_ia = ai_datos.get('pago', {})
        try:
            b_data = self.odoo.env['account.move'].read([bill_id], ['move_type', 'amount_residual', 'state', 'payment_state'])[0]
            if b_data['move_type'] == 'in_refund' or b_data['payment_state'] in ('paid', 'in_payment') or b_data['state'] != 'posted' or b_data.get('amount_residual', 0.0) <= 0:
                return ("", False, None)
            
            # Lógica Inteligente de Diario de Pago (Inyectada 09/06)
            journal_id = None
            try:
                if pago_ia.get('journal_id'):
                    journal_id = int(pago_ia['journal_id'])
                elif ctx.get('journals_preferidos'):
                    journal_id = ctx['journals_preferidos'][0]
            except Exception as e: 
                alertas.append(f"No se pudo determinar el diario de pago preferido, se usará el por defecto. ({str(e)})")

            if not journal_id:
                journal_id = 9 if datos_factura.get('moneda') == 'UYU' else 10

            df = ai_datos.get('datos_factura', {})
            pay_date = (df.get('fecha_pago_sugerida') or datos_factura['fecha_factura'])[:10]
            memo = f"Pago Factura {datos_factura['numero_factura']} - {ctx.get('vendedor', {}).get('razon_social', '')}"[:100]
            wiz_ctx = {'active_model': 'account.move', 'active_ids': [bill_id]}
            wiz_vals = {'journal_id': journal_id, 'amount': b_data['amount_residual'], 'payment_date': pay_date, 'communication': memo}
            wiz_id = self.odoo.execute_kw('account.payment.register', 'create', [[wiz_vals]], {'context': wiz_ctx})
            res_dict = self.odoo.execute_kw('account.payment.register', 'action_create_payments', [wiz_id], {'context': wiz_ctx})
            
            # Dejar rastro en el pago
            if res_dict and 'res_id' in res_dict:
                pay_id = res_dict['res_id']
                link_fac = f"<a href='https://prod17.odoo.imprentadiagonal.com.uy/web#id={bill_id}&model=account.move&view_type=form'>Factura {datos_factura['numero_factura']}</a>"
                msg_pago = f"🤖 <b>Pago Generado Automáticamente por IA</b><br>Vinculado a la {link_fac}.<br>Ver razonamiento de fechas, moneda y redondeo en el Chatter de la factura origen."
                self.odoo.env['mail.message'].create({'model': 'account.payment', 'res_id': pay_id, 'body': msg_pago, 'message_type': 'comment'})
            
            return (f"• <b>Resultado:</b> ✅ Pago registrado para el {pay_date}.", True, pay_date)
        except Exception as e: return (f"<b>💰 AVISO:</b> Error en registro de pago: {e}", False, None)

    def _reconciliar_nativamente(self, bill_id, payment_id=None, line_id=None):
        try:
            target_line_id = line_id
            if payment_id:
                p_data = self.odoo.env['account.payment'].read([payment_id], ['move_id'])[0]
                lines = self.odoo.env['account.move.line'].search([
                    ('move_id', '=', p_data['move_id'][0]), 
                    ('account_id.account_type', '=', 'liability_payable'), 
                    ('reconciled', '=', False)
                ], limit=1)
                if lines: target_line_id = lines[0]

            if target_line_id:
                # Upgrade Spano (Bajo Nivel): Conciliación nativa de líneas para soportar asientos manuales
                bill_lines = self.odoo.env['account.move.line'].search([
                    ('move_id', '=', bill_id),
                    ('account_id.account_type', '=', 'liability_payable'),
                    ('reconciled', '=', False)
                ], limit=1)
                
                if bill_lines:
                    self.odoo.execute_kw('account.move.line', 'reconcile', [[target_line_id, bill_lines[0]]])
                    return True
        except Exception as e:
            logging.error(f"Error en vinculación Spano: {e}")
        return False

    def _gestionar_nc_senior(self, nc_id, target_bill_id, total_nc):
        reporte = []
        bill = self.odoo.env['account.move'].read([target_bill_id], ['name', 'amount_total', 'amount_residual'])[0]
        
        # 1. BUSCAMOS LOS PAGOS ASOCIADOS USANDO EL RASTREO PROFUNDO Y LOS AJUSTAMOS PRIMERO
        payments = self._get_payments_from_bill(target_bill_id)
        if payments:
            for p in payments:
                if p['is_matched']:
                    reporte.append(f"⚠️ <b>AVISO:</b> El pago {p['name']} ya tiene extracto. No se pudo ajustar automáticamente.")
                else:
                    nuevo_monto_pago = bill['amount_total'] - total_nc
                    if nuevo_monto_pago <= 0.02:
                        self.odoo.env['account.payment'].action_draft([p['id']])
                        self.odoo.env['account.payment'].unlink([p['id']])
                        reporte.append(f"♻️ Pago {p['name']} eliminado (Factura totalmente cancelada por NC).")
                    else:
                        self.odoo.env['account.payment'].action_draft([p['id']])
                        self.odoo.env['account.payment'].write([p['id']], {'amount': nuevo_monto_pago})
                        self.odoo.env['account.payment'].action_post([p['id']])
                        self._reconciliar_nativamente(target_bill_id, p['id'])
                        reporte.append(f"⚖️ Pago {p['name']} ajustado a ${nuevo_monto_pago:.2f}.")

        # 2. AHORA QUE LOS PAGOS FUERON AJUSTADOS/BORRADOS, LAS LÍNEAS QUEDAN LIBRES PARA CONCILIAR LA NC
        nc_lines = self.odoo.env['account.move.line'].search([('move_id', '=', nc_id), ('account_id.account_type', '=', 'liability_payable'), ('reconciled', '=', False)], limit=100)
        bill_lines = self.odoo.env['account.move.line'].search([('move_id', '=', target_bill_id), ('account_id.account_type', '=', 'liability_payable'), ('reconciled', '=', False)], limit=100)
        if nc_lines and bill_lines:
            try:
                self.odoo.execute_kw('account.move.line', 'reconcile', [nc_lines + bill_lines])
                reporte.append(f"✅ NC conciliada contra factura {bill['name']}.")
            except Exception as e:
                logging.exception("Error conciliando NC:")
        
        return "<br>".join(reporte)

    def _buscar_en_ucfe_y_crear(self, rut_vendedor, serie, nro, tipo='111', fecha=None):
        from ucfe_connector import UCFESyncBot
        bot = UCFESyncBot()
        fecha_desde = "20260101"
        fecha_hasta = datetime.now().strftime("%Y%m%d")
        if fecha:
            try: 
                fecha_obj = datetime.strptime(fecha[:10], "%Y-%m-%d")
                fecha_desde = (fecha_obj - timedelta(days=5)).strftime("%Y%m%d")
                fecha_hasta = (fecha_obj + timedelta(days=5)).strftime("%Y%m%d")
            except: pass
        payload = {"rut": bot.user, "rutEmisor": rut_vendedor, "fechaDesde": fecha_desde, "fechaHasta": fecha_hasta, "tipoCfe": int(tipo), "pageSize": 50}
        try:
            logging.info(f"🔎 UCFE Búsqueda Autónoma: {serie}-{nro} de {rut_vendedor} entre {fecha_desde} y {fecha_hasta}...")
            res = requests.post(bot.url_list + "ObtenerCfeRecibidosInicial", json=payload, auth=bot.auth, timeout=40)
            import xml.etree.ElementTree as ET
            root = ET.fromstring(res.text.strip().replace('\ufeff', ''))
            target = None
            for c in root.findall(".//{*}CfeRecibido"):
                if c.findtext(".//{*}Serie") == serie and c.findtext(".//{*}Numero") == nro:
                    target = c; break
            if target:
                logging.info(f"📥 Factura origen {serie}-{nro} encontrada en UCFE. Descargando y procesando...")
                xml = bot._descargar_xml_puro(target, tipo_code=tipo)
                pdf = bot._descargar_pdf_puro(target, tipo_code=tipo)
                atts = [{'name': 'orig.xml', 'datas': base64.b64encode(xml.encode()).decode(), 'mimetype': 'application/xml'}, {'name': 'orig.pdf', 'datas': pdf, 'mimetype': 'application/pdf'}]
                res_ids = self.procesar_desde_zoho({'attachments': atts})
                return res_ids[0] if res_ids else None
            else:
                logging.warning(f"⚠️ No se encontró el documento en UCFE: {serie}-{nro}")
        except Exception as e:
            logging.error(f"❌ Error buscando en UCFE: {e}")
        return None

    def _handle_resguardo(self, d, attachments, ctx):
        rut_cfe = str(d.get('rut'))
        partner_id = ctx.get('vendedor', {}).get('id') if ctx.get('vendedor') else None
        if not partner_id and rut_cfe:
            p_ids = self.odoo.env['res.partner'].search(['|', ('vat', '=', rut_cfe), ('vat', '=', f"UY{rut_cfe}")], limit=100)
            if p_ids: partner_id = p_ids[0]
        if not partner_id: return None
        if d.get('referencias'): return self._proceder_cobro_resguardo(d, partner_id, attachments, ctx, f"Resguardo {d['numero_factura']}")
        return self._archivar_en_partner(d, partner_id, attachments, float(d['total']))

    def _proceder_cobro_resguardo(self, d, partner_id, attachments, ctx, ref_text):
        target_invoices = []
        open_invs = self.odoo.env['account.move'].search_read([('partner_id', '=', partner_id), ('move_type', '=', 'out_invoice'), ('state', '=', 'posted'), ('payment_state', 'in', ('not_paid', 'partial'))], ['id', 'name', 'ref'], limit=100)
        for ref in d.get('referencias', []):
            serie_nro = f"{ref.get('serie_ref')}-{ref.get('nro_cfe_ref')}"
            for inv in open_invs:
                if serie_nro in str(inv.get('name', '')) or serie_nro in str(inv.get('ref', '')): target_invoices.append(inv['id'])
        if not target_invoices: return self._archivar_en_partner(d, partner_id, attachments, float(d['total']), nota="No se encontraron facturas de venta vinculadas.")

        journal_code = 'RER'
        if '2183118' in d.get('codigos_retencion', []): journal_code = 'REE'
        else:
            prompt = f"Analiza resguardo {d}. Elige diario: REE (Estado), RER (IRPF), RET (Tarjetas). JSON 'codigo' y 'razonamiento'."
            res_ia = self.llamar_ia(prompt, json_mode=True)
            journal_code = res_ia.get('codigo', 'RER')

        journal_id = self.odoo.env['account.journal'].search([('code', '=', journal_code)], limit=1)[0]
        wiz_ctx = {'active_model': 'account.move', 'active_ids': target_invoices}
        wiz_vals = {'journal_id': journal_id, 'amount': float(d['total']), 'payment_date': d['fecha_factura'], 'communication': ref_text}
        wiz_id = self.odoo.execute_kw('account.payment.register', 'create', [[wiz_vals]], {'context': wiz_ctx})
        self.odoo.execute_kw('account.payment.register', 'action_create_payments', [wiz_id], {'context': wiz_ctx})
        pay_id = self.odoo.env['account.payment'].search([('ref', '=', ref_text)], limit=1, order='id desc')[0]
        links = ", ".join([f"<a href='https://prod17.odoo.imprentadiagonal.com.uy/web#id={i}&model=account.move&view_type=form'>Factura {i}</a>" for i in target_invoices])
        att_vals = [{'name': a['name'], 'datas': a['datas'], 'res_model': 'account.payment', 'res_id': pay_id, 'type': 'binary'} for a in attachments]
        att_ids = self.odoo.env['ir.attachment'].create(att_vals) if att_vals else []
        msg = f"<b>🛡️ E-RESGUARDO APLICADO</b><br>ORIGEN: UCFE<br>🧠 <b>Razonamiento Diario:</b> Clasificado como {journal_code}.<br><b>🧾 Facturas Afectadas:</b> {links}.<br><br><b>👀 REVISIÓN:</b>{self._get_atc_mentions()}"
        self.odoo.env['mail.message'].create({'model': 'account.payment', 'res_id': pay_id, 'body': msg, 'message_type': 'comment', 'attachment_ids': [(6, 0, att_ids)]})
        self._crear_actividad_atc(pay_id, 'account.payment', 'Revisar Resguardo', 'Validar vinculación.')
        return pay_id

    def _archivar_en_partner(self, d, partner_id, attachments, monto, nota=""):
        att_vals = [{'name': a['name'], 'datas': a['datas'], 'res_model': 'res.partner', 'res_id': partner_id, 'type': 'binary'} for a in attachments]
        att_ids = self.odoo.env['ir.attachment'].create(att_vals) if att_vals else []
        adicional = f"<br><b>Aviso:</b> {nota}" if nota else ""
        msg = f"<b>🛡️ E-RESGUARDO ARCHIVADO (Auditoría Día 10)</b><br>Resguardo {d['numero_factura']} por ${monto}.{adicional}<br><b>Acción:</b> Archivo inmediato para auditoría diferida al cierre de mes.{self._get_atc_mentions()}"
        self.odoo.env['mail.message'].create({'model': 'res.partner', 'res_id': partner_id, 'body': msg, 'message_type': 'comment', 'attachment_ids': [(6, 0, att_ids)]})
        self._crear_actividad_atc(partner_id, 'res.partner', 'Revisar Resguardo (Auditoría)', 'Se archivó resguardo para cotejar en el cierre del día 10.')
        return True

    def _gestionar_cobranza_senior(self, partner_id, datos_factura, attachments, ctx):
        """Camino Senior: Cobranzas con límite de 5 días, validación de RUT y alertas ATC obligatorias."""
        monto = float(datos_factura['total'])
        rut_esperado = ctx.get('vendedor', {}).get('vat')
        fecha_doc_str = datos_factura.get('fecha_factura')
        menciones = self._get_atc_mentions()
        
        # Filtro de fecha: +- 5 días
        domain = [
            ('partner_id.vat', '=', rut_esperado), 
            ('amount', '=', monto), 
            ('state', '=', 'posted'), 
            ('payment_type', '=', 'outbound')
        ]
        
        if fecha_doc_str:
            try:
                fecha_doc = datetime.strptime(fecha_doc_str[:10], '%Y-%m-%d')
                fecha_inicio = (fecha_doc - timedelta(days=5)).strftime('%Y-%m-%d')
                fecha_fin = (fecha_doc + timedelta(days=5)).strftime('%Y-%m-%d')
                domain.extend([('date', '>=', fecha_inicio), ('date', '<=', fecha_fin)])
            except: pass

        p_ids = self.odoo.env['account.payment'].search_read(domain, ['id', 'name', 'ref'], limit=1, order='date desc')
        
        if p_ids:
            pay = p_ids[0]
            att_vals = [{'name': a['name'], 'datas': a['datas'], 'res_model': 'account.payment', 'res_id': pay['id'], 'type': 'binary'} for a in attachments if 'pdf' in a['mimetype']]
            att_ids = self.odoo.env['ir.attachment'].create(att_vals) if att_vals else []
            nuevo_memo = f"{pay.get('ref') or ''} - Recibo {datos_factura['numero_factura']}"[:100]
            self.odoo.env['account.payment'].write([pay['id']], {'ref': nuevo_memo})
            
            msg = f"🛡️ <b>RECIBO DE COBRANZA VINCULADO</b><br>Se vinculó exitosamente el recibo {datos_factura['numero_factura']} por ${monto:.2f}.<br><br><b>👀 REVISIÓN:</b>{menciones}"
            self.odoo.env['mail.message'].create({'model': 'account.payment', 'res_id': pay['id'], 'body': msg, 'message_type': 'comment', 'attachment_ids': [(6, 0, att_ids)]})
            self._crear_actividad_atc(pay['id'], 'account.payment', f"Auditar Recibo {datos_factura['numero_factura']}", "Validar que el comprobante adjunto corresponda a este pago.")
            self._update_doc_state(str(datos_factura.get('rut', '')), str(datos_factura.get('tipo_cfe', '')), str(datos_factura.get('serie', '')), str(datos_factura.get('numero', '')), xml_ok=True, move_id=pay['id'])
            return f"✅ Recibo {datos_factura['numero_factura']} vinculado al pago {pay['name']}."
        else:
            self._update_doc_state(str(datos_factura.get('rut', '')), str(datos_factura.get('tipo_cfe', '')), str(datos_factura.get('serie', '')), str(datos_factura.get('numero', '')), xml_ok=True, move_id=None)
            msg = f"⚠️ <b>ALERTA DE COBRANZA HUÉRFANA</b><br>Se recibió el recibo <b>{datos_factura['numero_factura']}</b> por <b>${monto:.2f}</b>, pero NO se encontró un pago coincidente (mismo monto y fecha ±5 días) en Odoo para este proveedor.<br><br>Por favor, procesar y vincular manualmente.<br><br><b>🆘 AUXILIO:</b>{menciones}"
            self.odoo.env['mail.message'].create({'model': 'res.partner', 'res_id': partner_id, 'body': msg, 'message_type': 'comment'})
            self._crear_actividad_atc(partner_id, 'res.partner', f"Vincular Recibo {datos_factura['numero_factura']}", f"Llegó un recibo por ${monto:.2f} que no encontró pago automático.")
            return "⚠️ Pago no encontrado para cobranza. Se alertó al equipo ATC en el proveedor."

    def _get_atc_mentions(self):
        deps = self.odoo.env['hr.department'].search(['|', ('name', 'ilike', 'Admin'), ('name', 'ilike', 'ATC')], limit=100)
        emps = self.odoo.env['hr.employee'].search_read([('department_id', 'in', deps), ('user_id', '!=', False)], ['user_id'], limit=100)
        if not emps: return ""
        
        user_ids = [e['user_id'][0] for e in emps]
        users_data = self.odoo.env['res.users'].search_read([('id', 'in', user_ids), ('active', '=', True)], ['partner_id', 'name'], limit=100)
        
        html = ""
        for u in users_data:
            html += f"&nbsp;<a href='#' data-oe-model='res.partner' data-oe-id='{u['partner_id'][0]}'>@{u['name']}</a>"
        return html

    def _get_payments_from_bill(self, bill_id):
        """Rastrea pagos a través de reconciliaciones parciales (Regla de Oro)."""
        try:
            lines = self.odoo.env['account.move.line'].search_read([('move_id', '=', bill_id), ('account_id.account_type', '=', 'liability_payable')], ['matched_debit_ids', 'matched_credit_ids'], limit=100)
            m_ids = []
            for l in lines:
                m_ids.extend(l.get('matched_debit_ids', []))
                m_ids.extend(l.get('matched_credit_ids', []))
            if not m_ids: return []
            partials = self.odoo.env['account.partial.reconcile'].read(m_ids, ['debit_move_id', 'credit_move_id'])
            line_ids = [p['debit_move_id'][0] for p in partials if p.get('debit_move_id')] + [p['credit_move_id'][0] for p in partials if p.get('credit_move_id')]
            if not line_ids: return []
            rel_lines = self.odoo.env['account.move.line'].read(list(set(line_ids)), ['payment_id'])
            p_ids = list(set([l['payment_id'][0] for l in rel_lines if l.get('payment_id')]))
            return self.odoo.env['account.payment'].read(p_ids, ['id', 'name', 'amount', 'is_matched', 'reconciled_bill_ids']) if p_ids else []
        except Exception as e:
            logging.error(f"Error rastreando pagos: {e}")
            return []

    def _crear_actividad_atc(self, res_id, res_model, summary, note):
        deps = self.odoo.env['hr.department'].search(['|', ('name', 'ilike', 'Admin'), ('name', 'ilike', 'ATC')], limit=100)
        emps = self.odoo.env['hr.employee'].search_read([('department_id', 'in', deps), ('user_id', '!=', False)], ['user_id'], limit=100)
        if not emps: return
        user_ids = [e['user_id'][0] for e in emps]
        active_users = self.odoo.env['res.users'].search([('id', 'in', user_ids), ('active', '=', True)], limit=100)
        if not active_users: return
        mid = self.odoo.env['ir.model'].search([('model', '=', res_model)], limit=100)[0]
        deadline = datetime.now().strftime('%Y-%m-%d')
        activities = [{'res_model_id': mid, 'res_id': res_id, 'activity_type_id': 4, 'summary': summary, 'note': note, 'user_id': uid, 'date_deadline': deadline} for uid in active_users]
        try: self.odoo.env['mail.activity'].create(activities)
        except Exception as e: logging.warning(f"Error creando actividades ATC: {e}")

    def _calculate_accounting_date(self, invoice_date_str, alertas=None):
        if alertas is None: alertas = []
        if not invoice_date_str: return datetime.now().strftime("%Y-%m-%d")
        try: 
            inv_date = datetime.strptime(invoice_date_str, "%Y-%m-%d")
        except Exception as e: 
            alertas.append(f"La IA proporcionó una fecha inválida '{invoice_date_str}'. Se usó la fecha de hoy por defecto.")
            return datetime.now().strftime("%Y-%m-%d")
        today = datetime.now()
        return invoice_date_str if (inv_date.month == today.month or (today.day <= 7 and inv_date < today)) else today.strftime("%Y-%m-%d")

    def _mapear_impuesto(self, ind, alertas=None):
        if alertas is None: alertas = []
        """Mapeo dinámico de IndFact a Tax ID y Tasa (DGI Standard)."""
        ind = str(ind)
        if ind == '3': return 1, 0.22      # Tasa Básica
        if ind == '2': return 2, 0.10      # Tasa Mínima
        if ind in ('1', '6', '7'): return 7, 0.0 # Exento / No Gravado
        alertas.append(f"Código de impuesto desconocido '{ind}'. Se forzó a Exento por seguridad.")
        return 7, 0.0

    def _upsert_factura(self, partner_id, datos_factura, lineas_ia, attachments, source_type='XML', ctx=None, reasoning=None, existing_bill_id=None):
        reason_data = reasoning.get('analisis_contexto', {}) if isinstance(reasoning, dict) else {}
        ai_datos = reasoning.get('datos_factura', {}) if isinstance(reasoning, dict) else {}
        menciones = self._get_atc_mentions()
        
        # 1. CAMINO COBRANZA
        if reason_data.get('tipo_documento') == 'cobranza' or "cobranza" in str(datos_factura.get('lineas', [{}])[0].get('nom_item', '')).lower():
            return self._gestionar_cobranza_senior(partner_id, datos_factura, attachments, ctx), False

        tipo_cfe = str(datos_factura.get('tipo_cfe', '111'))
        is_nc = tipo_cfe in ('102', '112', '212')
        move_type = 'in_refund' if is_nc else 'in_invoice'
        
        # 2. CAPTURA DE IDENTIDAD Y PAGOS PREVIOS (Regla de Oro - ANTES DE TOCAR FACTURA)
        bill_id = existing_bill_id or self._get_bill_by_ref_flexible(partner_id, datos_factura['numero_factura'])
        existing_payments = []
        if bill_id:
            existing_payments = self._get_payments_from_bill(bill_id)
            if existing_payments:
                logging.info(f"💰 Se memorizaron {len(existing_payments)} pagos para re-vincular tras auditoría.")

        # 3. Preparar Líneas con Jerarquía XML, Prorrateo de Descuentos e Impuestos Explícitos
        suma_precios_base = sum(float(l.get('precio_original_xml', l.get('precio', 0.0))) * float(l.get('cantidad_xml', l.get('cantidad', 1.0))) for l in lineas_ia)
        global_disc_pct = 0.0
        for disc in datos_factura.get('descuentos_globales', []):
            if disc.get('tpo_mov') == 'D':
                val = float(disc.get('valor', 0.0))
                global_disc_pct += (val / suma_precios_base) * 100 if str(disc.get('tpo_dr')) == '1' and suma_precios_base > 0 else val

        invoice_lines = []
        mnt_bruto = datos_factura.get('mnt_bruto') == '1'
        alertas_bot = []
        for line in lineas_ia:
            if str(line.get('producto_odoo_id')) == '14337': continue
            
            # 1. Recuperar datos clave del XML
            monto_item_xml = float(line.get('monto_item_xml', 0.0))
            cantidad = float(line.get('cantidad_xml', line.get('cantidad', 1.0)))
            ind = str(line.get('ind_fact_xml', line.get('ind_fact', '3')))
            nom = line.get('descripcion', '').lower()

            # 2. FILTROS DE DESCARTE (Limpieza DGI de 50 Facturas)
            # Solo filtramos ceros en Facturas (111) que NO vengan de OCR manual.
            # Los remitos y tickets manuales deben conservar sus líneas aunque pesen $0.
            if "redondeo" in nom:
                continue
            if monto_item_xml == 0 and str(tipo_cfe) not in ('181', '281') and source_type != 'PDF_OCR':
                continue

            # 3. MAPEO DE IVA DINÁMICO
            tax_id, rate = self._mapear_impuesto(ind)

            # 4. CÁLCULO DE PRECIO UNITARIO (La Verdad del MontoItem)
            precio_base = monto_item_xml / cantidad if cantidad > 0 else monto_item_xml
            precio_odoo = precio_base / (1 + rate) if (mnt_bruto and rate > 0) else precio_base
            
            # 5. DESCUENTOS (Solo Globales, los de línea ya están en el MontoItem)
            line_disc = round(global_disc_pct, 4)
            
            # Mapeo Senior de Cuenta Contable con Fallback
            acc_id = line.get('cuenta_contable_odoo_id') or line.get('account_id')
            
            # 1. Validar que la cuenta sugerida (si la hay) realmente exista en Odoo
            if acc_id:
                try:
                    exists = self.odoo.env['account.account'].search_count([('id', '=', int(acc_id))])
                    if exists == 0:
                        logging.warning(f"⚠️ La cuenta {acc_id} sugerida por la IA ya no existe en Odoo. Buscando alternativa...")
                        acc_id = None
                except Exception:
                    acc_id = None

            if not acc_id:
                # Si la IA no la mandó o no existe, buscamos la del producto
                prod_id = line.get('producto_odoo_id')
                if prod_id:
                    try:
                        p_data = self.odoo.env['product.product'].read([prod_id], ['property_account_expense_id'])[0]
                        if p_data.get('property_account_expense_id'):
                            acc_id = p_data['property_account_expense_id'][0]
                    except: pass
                    
                if not acc_id:
                    # Último recurso: primera cuenta de gastos que encontremos
                    found_acc = self.odoo.env['account.account'].search([('account_type', '=', 'expense')], limit=1)
                    if not found_acc: found_acc = self.odoo.env['account.account'].search([('code', '=ilike', '5%')], limit=1)
                    if found_acc: acc_id = found_acc[0]

            invoice_lines.append((0, 0, {
                'name': line.get('descripcion', 'Sin descripción'), 
                'product_id': line.get('producto_odoo_id'),
                'account_id': acc_id,
                'quantity': cantidad, 
                'price_unit': precio_odoo, 
                'discount': line_disc, 
                'tax_ids': [(6, 0, [tax_id])] if tax_id else []
            }))

        currency_id = 2 if datos_factura.get('moneda') == 'USD' else 46
        
        # Redondeo Nativo de Odoo (Trigger Senior)
        monto_nf = float(datos_factura.get('monto_nf', 0.0))
        necesita_redondeo = monto_nf != 0 or datos_factura.get('redondeo_requerido_xml') or ai_datos.get('redondeo_requerido')
        
        vals = {
            'partner_id': partner_id, 
            'ref': datos_factura['numero_factura'], 
            'invoice_date': datos_factura['fecha_factura'], 
            'date': self._calculate_accounting_date(datos_factura['fecha_factura'], alertas_bot), 
            'move_type': move_type, 
            'currency_id': currency_id, 
            'invoice_line_ids': [(5, 0, 0)] + invoice_lines, 
            'invoice_cash_rounding_id': 1 if necesita_redondeo else False
        }

        if bill_id:
            # Solo pasamos a borrador si no lo está ya (Fix 10/06)
            curr_state = self.odoo.env['account.move'].read([bill_id], ['state'])[0]['state']
            if curr_state != 'draft':
                self.odoo.env['account.move'].button_draft([bill_id])
            self.odoo.env['account.move'].write([bill_id], vals)
        else:
            bill_id = self.odoo.env['account.move'].create(vals)
        self.odoo.env['account.move'].action_post([bill_id])

        # 4. GESTIÓN DE PAGO (SOLO SOBRE IDS CAPTURADOS O NUEVO)
        payment_msg = ""
        pago_gestionado = False
        razon_pag = reasoning.get('analisis_contexto', {}).get('analisis_pago_historico', 'N/A')
        
        if is_nc:
            target_id = None
            ref_buscada = "No especificada"
            for ref in datos_factura.get('referencias', []):
                if ref.get('tpo_doc_ref') in ('101', '111'):
                    ref_buscada = f"{ref.get('serie_ref')}-{ref.get('nro_cfe_ref')}"
                    target_id = self._get_bill_by_ref_flexible(partner_id, ref_buscada)
                    if not target_id: target_id = self._buscar_en_ucfe_y_crear(datos_factura['rut'], ref.get('serie_ref'), ref.get('nro_cfe_ref'))
            
            if target_id: 
                payment_msg = self._gestionar_nc_senior(bill_id, target_id, float(datos_factura['total']))
                pago_gestionado = True
            else: 
                payment_msg = f"⚠️ <b>NC Huérfana:</b> No se encontró la factura de origen <b>{ref_buscada}</b> en Odoo ni en UCFE. Se requiere vinculación manual."
            
            razon_pag = "Nota de crédito para aplicación inmediata contra saldos pendientes. No sujeta a programación de pagos."
        else:
            # REGLA DE ORO: Auditoría de Pagos Memorizados (Casos A, B y C)
            if existing_payments:
                updated_bill = self.odoo.env['account.move'].read([bill_id], ['amount_total'])[0]
                for p_data in existing_payments:
                    p_id = p_data['id']
                    other_bills_ids = [b_id for b_id in p_data.get('reconciled_bill_ids', []) if b_id != bill_id]
                    if other_bills_ids:
                        other_bills = self.odoo.env['account.move'].read(other_bills_ids, ['amount_total'])
                        other_total = sum(b.get('amount_total', 0) for b in other_bills)
                    else:
                        other_total = 0
                    remanente = p_data['amount'] - other_total
                    
                    if abs(remanente - updated_bill['amount_total']) < 0.02:
                        self._reconciliar_nativamente(bill_id, p_id)
                        payment_msg += f"• Pago {p_data['name']} re-vinculado exitosamente (Caso A)."
                        pago_gestionado = True
                    
                    elif p_data.get('is_matched'):
                        self._reconciliar_nativamente(bill_id, p_id)
                        diff = updated_bill['amount_total'] - remanente
                        payment_msg += f"<br>⚠️ <b>ALERTA CRÍTICA (Caso B):</b> Montos cambiaron pero el pago ya tiene extracto bancario. Diferencia de ${diff:.2f}.<br><b>🆘 AUXILIO HUMANOS:</b> {menciones}"
                        self._crear_actividad_atc(bill_id, 'account.move', f"URGENTE: Ajustar Pago {p_data['name']}", f"El pago tiene extracto pero la factura cambió su total en ${diff:.2f}.")
                        pago_gestionado = True
                    
                    else:
                        new_total_p = other_total + updated_bill['amount_total']
                        self.odoo.env['account.payment'].action_draft([p_id])
                        self.odoo.env['account.payment'].write([p_id], {'amount': new_total_p})
                        self.odoo.env['account.payment'].action_post([p_id])
                        for b_id in (other_bills_ids + [bill_id]): 
                            self._reconciliar_nativamente(b_id, p_id)
                        payment_msg += f"<br>♻️ <b>Ajuste Pago (Caso C):</b> Monto de {p_data['name']} actualizado a ${new_total_p:.2f} para cuadrar con auditoría XML."
                        pago_gestionado = True
            
            if not pago_gestionado:
                # Solo si llegamos aquí sin haber tocado un pago previo, creamos uno nuevo
                msg, _, _ = self._programar_pago_senior(bill_id, partner_id, datos_factura, ctx, ai_datos=reasoning, alertas=alertas_bot)
                payment_msg = msg

        msg_body = f"""<b>🚀 DOCUMENTO PROCESADO (Senior Audit)</b><br><br>
        <b>🧠 RAZONAMIENTO DUPLA:</b><br>{reason_data.get('mapeo_duplas', 'N/A')}<br>
        <b>📏 REDONDEO:</b><br>{reason_data.get('auditoria_redondeo', 'N/A')}<br>
        <b>💳 PAGO/CRÉDITO:</b><br>{razon_pag}<br><br>
        {payment_msg}<br><br>"""
        
        if alertas_bot:
            msg_body += f"<b>⚠️ OBSERVACIONES DEL BOT:</b><ul>"
            for al in alertas_bot:
                msg_body += f"<li>{al}</li>"
            msg_body += "</ul><br>"
            
        msg_body += f"<b>👀 REVISIÓN:</b>{menciones}"
        
        att_vals = [{'name': a['name'], 'datas': a['datas'], 'res_model': 'account.move', 'res_id': bill_id, 'type': 'binary'} for a in attachments]
        att_ids = self.odoo.env['ir.attachment'].create(att_vals) if att_vals else []
        self.odoo.env['mail.message'].create({'model': 'account.move', 'res_id': bill_id, 'body': msg_body, 'message_type': 'comment', 'attachment_ids': [(6, 0, att_ids)]})
        self._crear_actividad_atc(bill_id, 'account.move', 'Revisar factura automática', 'Validar asignación.')
        return (bill_id, False)

    def _descargar_solo_pdf_ucfe(self, rut_vendedor, serie, nro, tipo='111'):
        from ucfe_connector import UCFESyncBot
        try:
            bot = UCFESyncBot()
            fecha_desde = (datetime.now() - timedelta(days=60)).strftime("%Y%m%d")
            fecha_hasta = datetime.now().strftime("%Y%m%d")
            payload = {"rut": bot.user, "rutEmisor": rut_vendedor, "fechaDesde": fecha_desde, "fechaHasta": fecha_hasta, "tipoCfe": int(tipo), "pageSize": 50}
            res = requests.post(bot.url_list + "ObtenerCfeRecibidosInicial", json=payload, auth=bot.auth, timeout=40)
            import xml.etree.ElementTree as ET
            root = ET.fromstring(res.text.strip().replace('\ufeff', ''))
            for c in root.findall(".//{*}CfeRecibido"):
                if c.findtext(".//{*}Serie") == serie and c.findtext(".//{*}Numero") == nro:
                    return bot._descargar_pdf_puro(c, tipo_code=tipo)
        except Exception as e:
            logging.exception("Error descargando solo PDF de UCFE:")
        return None

    def llamar_ia(self, prompt_text, json_mode=True, attachments=None, is_retry=False):
        from google.genai import types
        parts = [prompt_text]
        if attachments:
            for att in attachments:
                if att.get('datas') and ('image' in att['mimetype'] or 'pdf' in att['mimetype'] or 'xml' in att['mimetype']):
                    parts.append(types.Part.from_bytes(data=base64.b64decode(att['datas']), mime_type=att['mimetype'] if 'xml' not in att['mimetype'] else 'text/plain'))
        
        # Aumentamos explícitamente el límite de tokens de salida a 8192 para evitar truncamientos
        res = self.client.models.generate_content(
            model=self.model_name, 
            contents=parts, 
            config=types.GenerateContentConfig(
                candidate_count=1, 
                max_output_tokens=8192,
                response_mime_type="application/json" if json_mode else "text/plain"
            )
        )
        
        u = res.usage_metadata
        logging.info(f"📊 TOKENS: In: {u.prompt_token_count} | Out: {u.candidates_token_count} | Total: {u.total_token_count}")
        
        response_text = res.text
        if json_mode:
            response_text = re.sub(r'```json\s?|\s?```', '', response_text).strip()
            try:
                return json.loads(response_text)
            except Exception as e:
                if not is_retry:
                    logging.warning(f"⚠️ Error parseando JSON de IA ({e}). Iniciando mecanismo de auto-curación (Auto-Healing)...")
                    prompt_correccion = f"El siguiente texto debía ser un JSON válido pero tiene errores de formato. Por favor, NO cambies absolutamente nada de la información ni de los textos de análisis, simplemente arréglale el formato (llaves, comas, comillas) para que sea un JSON 100% válido.\n\nTEXTO ROTO:\n{response_text}"
                    return self.llamar_ia(prompt_correccion, json_mode=True, is_retry=True)
                else:
                    logging.error(f"❌ Fallo crítico de auto-curación JSON: {e}. Respuesta cruda final: {response_text}")
                    return {}
        return response_text

    def _obtener_fecha_pago_real(self, target_id, is_payment):
        try:
            if is_payment:
                return str(self.odoo.env['account.payment'].read([target_id], ['date'])[0]['date'])
            # Buscar pagos vinculados
            payments = self.odoo.env['account.payment'].search_read([('reconciled_bill_ids', 'in', [target_id])], ['date'], order='date desc', limit=1)
            if payments: return str(payments[0]['date'])
            # Si no hay pagos, fecha de vencimiento o factura
            rec = self.odoo.env['account.move'].read([target_id], ['invoice_date_due', 'invoice_date'])[0]
            return str(rec.get('invoice_date_due') or rec.get('invoice_date') or 'A Confirmar')
        except: return 'A Confirmar'

    def procesar_factura(self, ticket_id):
        """Procesa una factura desde un ticket de Helpdesk (PDF/Imagen)."""
        self.conectar()
        ticket_data = self.odoo.env['helpdesk.ticket'].read([ticket_id], ['name', 'description'])
        if not ticket_data:
            logging.warning(f"⚠️ Ticket #{ticket_id} no encontrado en Odoo.")
            return False
        ticket_data = ticket_data[0]
        logging.info(f"🚀 Analizando ticket #{ticket_id}: {ticket_data['name']}")
        
        atts = self.odoo.env['ir.attachment'].search_read([('res_model', '=', 'helpdesk.ticket'), ('res_id', '=', ticket_id)], ['name', 'mimetype', 'datas'], limit=100)
        if not atts:
            logging.warning(f"⚠️ Ticket #{ticket_id} no tiene adjuntos.")
            return False

        # Intentamos encontrar un PDF o Imagen para la IA
        factura_att = next((a for a in atts if 'pdf' in a['mimetype'] or 'image' in a['mimetype']), None)
        if not factura_att:
            logging.warning(f"⚠️ Ticket #{ticket_id} no tiene PDF o Imagen procesable.")
            return False

        catalog_json = json.dumps(self.odoo.env['product.product'].search_read([('purchase_ok', '=', True)], ['id', 'name'], limit=500), separators=(',', ':'))
        accounts_json = json.dumps(self.odoo.env['account.account'].search_read([('account_type', 'in', ('expense', 'expense_direct_cost'))], ['id', 'name', 'code'], limit=500), separators=(',', ':'))
        with open('PROMPT_AGENTE_COMPRAS.md', 'r', encoding='utf-8') as f: p_m = f.read()

        prompt_init = f"{p_m}\nCatálogo de Productos: {catalog_json}\nCatálogo de Cuentas de Gasto: {accounts_json}\nExtrae los datos básicos y líneas de este documento."
        mapping = self.llamar_ia(prompt_init, attachments=[factura_att])
        
        if not isinstance(mapping, dict):
            logging.warning(f"⚠️ Ticket #{ticket_id}: La IA no devolvió un JSON válido.")
            return False

        df = mapping.get('datos_factura') or {}
        rut = df.get('proveedor_rut')
        
        # --- LÓGICA UNIVERSAL: DB -> Odoo -> UCFE -> OCR (Tickets) ---
        tipo_doc_texto = str(df.get('tipo_documento', '')).lower()
        is_resguardo = 'resguardo' in tipo_doc_texto or str(df.get('tipo_cfe')) in ('182', '282')
        tipo_cfe = str(df.get('tipo_cfe', '182' if is_resguardo else '111'))
        
        serie = str(df.get('serie_factura', df.get('numero_factura', 'A')[:1] if 'A' in df.get('numero_factura', '') else 'A'))
        nro = "".join(re.findall(r'\d+', str(df.get('numero_factura', ''))))
        
        # Corrección de RUT si la IA extrajo el nuestro por ser Resguardo o por error
        empresa_rut = os.getenv('EMPRESA_RUT', '213382910014')
        if not rut or rut == empresa_rut:
            prompt_rescate = f"Dime SOLO el número de RUT del EMISOR (el que vende o retiene). Responde solo con los 12 dígitos numéricos, nada más."
            rut_emisor = self.llamar_ia(prompt_rescate, json_mode=False, attachments=[factura_att]).strip()
            rut_emisor = "".join(re.findall(r'\d+', rut_emisor))
            if len(rut_emisor) >= 11:
                rut = rut_emisor
                
        # 1. Búsqueda en DB y Odoo, y luego UCFE si es CFE válido (RUT y Nro presentes)
        if rut and nro:
            doc_state = self._get_doc_state(rut, tipo_cfe, serie, nro)
            if doc_state['xml_ok'] and doc_state['odoo_move_id']:
                logging.info(f"⏭️ El documento {serie}-{nro} de {rut} ya fue procesado previamente. Solo vinculamos al ticket.")
                
                # --- RESCATE DE PDF DESDE TICKETS (Como en Zoho) ---
                target_id = doc_state['odoo_move_id']
                target_model = 'account.payment' if is_resguardo else 'account.move'
                
                if not is_resguardo:
                    exists_in_move = self.odoo.env['account.move'].search_count([('id', '=', target_id)])
                    if exists_in_move == 0: target_model = 'account.payment'

                if factura_att and not doc_state['pdf_proveedor_ok']:
                    logging.info(f"💾 PDF original encontrado en Ticket #{ticket_id}. Adjuntando a Odoo ({target_model} ID {target_id}).")
                    try:
                        att_vals = [{'name': factura_att['name'], 'datas': factura_att['datas'], 'res_model': target_model, 'res_id': target_id, 'type': 'binary'}]
                        att_ids = self.odoo.env['ir.attachment'].create(att_vals) if att_vals else []
                        msg_adj = f"<b>📄 PDF DE PROVEEDOR (Vía Helpdesk Ticket #{ticket_id})</b><br>Se ha recibido y adjuntado el PDF original."
                        self.odoo.env['mail.message'].create({'model': target_model, 'res_id': target_id, 'body': msg_adj, 'message_type': 'comment', 'attachment_ids': [(6, 0, att_ids)]})
                        self._update_doc_state(rut, tipo_cfe, serie, nro, pdf_prov_ok=True)
                    except Exception as e:
                        logging.exception("Error al adjuntar PDF rezagado desde Ticket:")
                # --------------------------------------------------

                msg = f"<b>🧾 DOCUMENTO PREVIAMENTE PROCESADO</b>: Este documento ya existe en contabilidad.<br><br>👉 <a href='https://prod17.odoo.imprentadiagonal.com.uy/web#id={target_id}&model={target_model}&view_type=form'>Ver en Odoo</a>"
                self.odoo.env['mail.message'].create({'model': 'helpdesk.ticket', 'res_id': ticket_id, 'body': msg, 'message_type': 'comment', 'subtype_id': 2})
                
                borrador = f"<b>✍️ BORRADOR DE RESPUESTA (Copiar y Pegar)</b><br>Estimado proveedor, muchas gracias por su envío. Le confirmamos que el documento {serie}-{nro} ha sido recibido y se encuentra procesado correctamente en nuestro sistema.<br>Saludos cordiales."
                self.odoo.env['mail.message'].create({'model': 'helpdesk.ticket', 'res_id': ticket_id, 'body': borrador, 'message_type': 'comment', 'subtype_id': 2})
                
                return target_id
                
            logging.info(f"🛡️ Buscando documento en UCFE (Emisor: {rut}, Serie: {serie}, Nro: {nro}, Tipo: {tipo_cfe})...")
            res_id = self._buscar_en_ucfe_y_crear(rut, serie, nro, tipo=tipo_cfe, fecha=df.get('fecha_factura'))
            if res_id:
                msg = f"<b>🛡️ DOCUMENTO DETECTADO Y PROCESADO VÍA UCFE</b>: Se descargó el XML original para su procesamiento perfecto.<br><br>👉 <a href='https://prod17.odoo.imprentadiagonal.com.uy/web#id={res_id}&model={'account.payment' if is_resguardo else 'account.move'}&view_type=form'>Ver Registro en Odoo</a>"
                self.odoo.env['mail.message'].create({'model': 'helpdesk.ticket', 'res_id': ticket_id, 'body': msg, 'message_type': 'comment', 'subtype_id': 2})
                
                pay_date = self._obtener_fecha_pago_real(res_id, is_resguardo)
                pago_str = f", y se ha programado su pago para la fecha {pay_date}" if not is_resguardo and pay_date != 'A Confirmar' else ""
                borrador = f"<b>✍️ BORRADOR DE RESPUESTA (Copiar y Pegar)</b><br>Estimado proveedor, muchas gracias por su envío. Le confirmamos que el documento {serie}-{nro} ha sido recibido y se encuentra procesado correctamente en nuestro sistema{pago_str}.<br>Saludos cordiales."
                self.odoo.env['mail.message'].create({'model': 'helpdesk.ticket', 'res_id': ticket_id, 'body': borrador, 'message_type': 'comment', 'subtype_id': 2})
                
                return res_id
            else:
                logging.warning(f"⚠️ No se encontró XML en UCFE para {serie}-{nro}. Utilizando fallback OCR de la IA...")
        
        # 2. Fallback: OCR IA Pura (_upsert_factura) - Solo si UCFE falla o no es CFE
        ctx = self._get_vendor_context(rut)
        partner_id = ctx['vendedor']['id'] if ctx['vendedor'] else None
        
        if not partner_id:
            msg = f"⚠️ <b>ALERTA DE PROCESAMIENTO OCR</b><br>UCFE no encontró el documento y el bot <b>no pudo encontrar un proveedor en Odoo</b> coincidente con el RUT <b>{rut or 'No detectado'}</b>.<br>El documento no fue creado en contabilidad. Por favor, ingresarlo manualmente."
            self.odoo.env['mail.message'].create({'model': 'helpdesk.ticket', 'res_id': ticket_id, 'body': msg, 'message_type': 'comment', 'subtype_id': 2})
            logging.warning(f"⚠️ Ticket #{ticket_id}: No se encontró el proveedor con RUT {rut}. Se cancela creación OCR.")
            return False

        datos_cfe = {
            'rut': rut,
            'numero_factura': df.get('numero_factura', 'S/N'),
            'fecha_factura': df.get('fecha_factura'),
            'moneda': df.get('moneda', 'UYU'),
            'total': df.get('monto_total', 0.0),
            'tipo_cfe': tipo_cfe,
        }

        new_id, _ = self._upsert_factura(partner_id, datos_cfe, mapping.get('lineas', []), atts, 'PDF_OCR', ctx, reasoning=mapping)
        
        base_url = f"https://prod17.odoo.imprentadiagonal.com.uy/web#id={new_id}&model={'account.payment' if is_resguardo else 'account.move'}&view_type=form"
        msg = f"<b>🧾 DOCUMENTO PROCESADO POR OCR (IA)</b>: UCFE no arrojó resultados, se procesó extrayendo datos del PDF.<br><br>👉 <a href='{base_url}'>Ver Registro en Odoo</a>"
        self.odoo.env['mail.message'].create({'model': 'helpdesk.ticket', 'res_id': ticket_id, 'body': msg, 'message_type': 'comment', 'subtype_id': 2})
        
        pay_date = self._obtener_fecha_pago_real(new_id, is_resguardo)
        pago_str = f", y se ha programado su pago para la fecha {pay_date}" if not is_resguardo and pay_date != 'A Confirmar' else ""
        borrador = f"<b>✍️ BORRADOR DE RESPUESTA (Copiar y Pegar)</b><br>Estimado proveedor, muchas gracias por su envío. Le confirmamos que el documento {serie}-{nro} ha sido recibido y se encuentra procesado correctamente en nuestro sistema{pago_str}.<br>Saludos cordiales."
        self.odoo.env['mail.message'].create({'model': 'helpdesk.ticket', 'res_id': ticket_id, 'body': borrador, 'message_type': 'comment', 'subtype_id': 2})
        
        return new_id

    def procesar_desde_zoho(self, email_data):
        self.conectar()
        xml_att = next((a for a in email_data['attachments'] if 'xml' in a['name'].lower()), None)
        if not xml_att: return False
        docs = CFEExpertParser.parse_xml(base64.b64decode(xml_att['datas']))
        logging.info(f"🔎 CFEExpertParser devolvió {len(docs)} documentos para el XML adjunto.")
        catalog_json = json.dumps(self.odoo.env['product.product'].search_read([('purchase_ok', '=', True)], ['id', 'name'], limit=500), separators=(',', ':'))     
        with open('PROMPT_AGENTE_COMPRAS.md', 'r', encoding='utf-8') as f: p_m = f.read()
        ids = []
        for d in docs:
            doc_state = self._get_doc_state(str(d.get('rut', '')), str(d.get('tipo_cfe', '')), str(d.get('serie', '')), str(d.get('numero', '')))
            if doc_state['xml_ok']:
                logging.info(f"⏭️ Saltando documento {d.get('serie')}-{d.get('numero')} porque ya figura como PROCESADO (xml_ok=True) en agent_state.db.")
                # Rescate de PDF rezagado (Proveedor mandó el correo después que UCFE procesó)
                pdf_atts = [a for a in email_data['attachments'] if 'pdf' in a['name'].lower()]
                if pdf_atts and not doc_state['pdf_proveedor_ok']:
                    target_model = 'account.move'
                    target_id = doc_state['odoo_move_id']
                    
                    tipo_cfe_str = str(d.get('tipo_cfe', ''))
                    # Si es Resguardo (182, 282), el target_id guardado podría ser nulo si se archivó en el partner, o ser el ID del pago
                    if tipo_cfe_str in ('182', '282'):
                        if target_id:
                            target_model = 'account.payment'
                        else:
                            # Buscar partner_id si quedó archivado en el contacto
                            p_ids = self.odoo.env['res.partner'].search(['|', ('vat', '=', str(d.get('rut', ''))), ('vat', '=', f"UY{d.get('rut', '')}")], limit=100)
                            if p_ids:
                                target_model = 'res.partner'
                                target_id = p_ids[0]
                    # Si es una factura pero fue clasificada internamente como cobranza, odoo_move_id guardó el ID del account.payment
                    elif target_id:
                        # Hacemos una comprobación rápida para ver si ese ID corresponde a un pago o a una factura
                        # El ID de account.move y account.payment pueden colisionar, pero por lo general,
                        # sabemos que si el rut es de un cliente y tiene factura, podría ser cobranza.
                        # Para ser seguros, chequeamos si existe en account.move
                        exists_in_move = self.odoo.env['account.move'].search_count([('id', '=', target_id)])
                        if exists_in_move == 0:
                            # Si no está en account.move, asumimos que es el ID de un account.payment devuelto por _gestionar_cobranza_senior
                            target_model = 'account.payment'

                    if target_id:
                        logging.info(f"💾 PDF original encontrado en Zoho para documento {d.get('numero')}. Adjuntando a Odoo ({target_model} ID {target_id}).")
                        try:
                            att_vals = [{'name': a['name'], 'datas': a['datas'], 'res_model': target_model, 'res_id': target_id, 'type': 'binary'} for a in pdf_atts]
                            att_ids = self.odoo.env['ir.attachment'].create(att_vals) if att_vals else []
                            msg = f"<b>📄 PDF DE PROVEEDOR (Zoho)</b><br>Se ha recibido y adjuntado el PDF original."
                            self.odoo.env['mail.message'].create({'model': target_model, 'res_id': target_id, 'body': msg, 'message_type': 'comment', 'attachment_ids': [(6, 0, att_ids)]})
                            self._update_doc_state(str(d.get('rut', '')), str(d.get('tipo_cfe', '')), str(d.get('serie', '')), str(d.get('numero', '')), pdf_prov_ok=True)
                        except Exception as e:
                            logging.exception("Error al adjuntar PDF rezagado desde Zoho:")
                continue
            
            ctx = self._get_vendor_context(d['rut'])
            partner_id = ctx['vendedor']['id'] if ctx['vendedor'] else None
            
            if not partner_id:
                logging.warning(f"⚠️ Documento {d.get('serie')}-{d.get('numero')} ignorado: El RUT {d['rut']} no existe en Odoo.")
                continue

            bill_id_manual = None
            nro_str = str(d.get('numero', ''))
            bill_id_manual = self._get_bill_by_ref_flexible(partner_id, nro_str)
            if bill_id_manual:
                logging.info(f"🔍 Factura {nro_str} ya existe en Odoo (ID {bill_id_manual}). Procediendo a Auditoría XML para asegurar integridad.")
            
            if str(d.get('tipo_cfe')) in ('182', '282'):
                ids.append(self._handle_resguardo(d, email_data['attachments'], ctx))
                self._update_doc_state(str(d.get('rut', '')), str(d.get('tipo_cfe', '')), str(d.get('serie', '')), str(d.get('numero', '')), xml_ok=True, move_id=None)
                continue
                
            mapping = self.llamar_ia(f"{p_m}\nXML: {json.dumps(d, separators=(',', ':'))}\nHistorial: {json.dumps(ctx['historial'], separators=(',', ':'))}\nCatálogo: {catalog_json}")
            
            if not isinstance(mapping, dict) or not mapping:
                logging.warning(f"⚠️ Documento {d.get('serie')}-{d.get('numero')}: La IA no devolvió un JSON válido. Se omitirá el procesamiento inteligente de este CFE.")
                continue

            # VINCULACIÓN XML-DRIVER: El XML manda sobre las líneas, la IA etiqueta.
            final_lines = []
            ia_suggestions = list(mapping.get('lineas', []))
            redondeo_detectado = False
            tipo_cfe_actual = str(d.get('tipo_cfe', '111'))

            for xml_line in d['lineas']:
                nom_xml = str(xml_line.get('nom_item', '')).lower()
                monto_xml = float(xml_line.get('monto_item', 0))
                
                # 1. Filtro y Detección de Redondeo (Respetando Remitos)
                if "redondeo" in nom_xml:
                    redondeo_detectado = True
                    continue
                if monto_xml == 0 and tipo_cfe_actual not in ('181', '281'):
                    continue

                # 2. Match con la IA para obtener producto/cuenta
                matched = {}
                for i, sugg in enumerate(ia_suggestions):
                    # Match por descripción parcial o precio idéntico
                    if sugg.get('descripcion', '').lower() in nom_xml or abs(float(sugg.get('precio_unitario', 0)) - float(xml_line.get('precio_unitario', 0))) < 0.1:
                        matched = ia_suggestions.pop(i)
                        break
                
                if not matched and ia_suggestions:
                    # Si queda solo una sugerencia y es la última línea del XML, forzamos el match
                    if len(ia_suggestions) == 1:
                        matched = ia_suggestions.pop(0)

                consolidated = {
                    'descripcion': xml_line.get('nom_item', matched.get('descripcion', 'Sin descripción')),
                    'producto_odoo_id': matched.get('producto_odoo_id'),
                    'cuenta_contable_odoo_id': matched.get('cuenta_contable_odoo_id') or matched.get('account_id'),
                    'cantidad_xml': float(xml_line.get('cantidad', 1.0)),
                    'precio_original_xml': float(xml_line.get('precio_unitario', 0.0)),
                    'ind_fact_xml': xml_line.get('ind_fact', '3'),
                    'monto_item_xml': monto_xml
                }
                final_lines.append(consolidated)

            d['redondeo_requerido_xml'] = redondeo_detectado
            
            # Auditoría de Adjuntos: Si falta el PDF, lo buscamos en UCFE (Proactividad Senior)
            tiene_pdf = any('pdf' in a['name'].lower() for a in email_data['attachments'])
            if not tiene_pdf:
                logging.info(f"💾 PDF ausente para {d['numero_factura']}. Intentando descarga desde UCFE...")
                pdf_data = self._descargar_solo_pdf_ucfe(d['rut'], d['serie'], d['numero'], d['tipo_cfe'])
                if pdf_data:
                    email_data['attachments'].append({
                        'name': f"UCFE_{d['serie']}_{d['numero']}.pdf",
                        'datas': pdf_data,
                        'mimetype': 'application/pdf'
                    })
                    logging.info("✅ PDF recuperado de UCFE con éxito.")

            new_id, _ = self._upsert_factura(ctx['vendedor']['id'] if ctx['vendedor'] else None, d, final_lines, email_data['attachments'], 'XML', ctx, reasoning=mapping, existing_bill_id=bill_id_manual)
            self._update_doc_state(str(d.get('rut', '')), str(d.get('tipo_cfe', '')), str(d.get('serie', '')), str(d.get('numero', '')), xml_ok=True, move_id=new_id)
            ids.append(new_id)
        return ids

if __name__ == "__main__": pass
