import os
import json
import logging
import odoorpc
import base64
import re
from datetime import datetime, timedelta
from google import genai
from dotenv import load_dotenv
from cfe_expert_parser import CFEExpertParser

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("NestaSeniorCore")

class NestaSeniorCore:
    def __init__(self, odoo_client=None):
        self.odoo = odoo_client
        self.host = 'prod17.odoo.imprentadiagonal.com.uy'
        self.db = 'odoo17_prod'
        self.user = 'ylan.archimowicz@imprentadiagonal.com.uy'
        self.password = os.getenv('ODOO_PASSWORD', '9a50ca725dd8c0e451e8005982589dcdf3bf8fe7')
        self.api_key = os.getenv("GEMINI_API_KEY")
        if self.api_key:
            self.genai_client = genai.Client(api_key=self.api_key)
            self.model_name = 'gemini-3.5-flash'

    def conectar(self):
        if not self.odoo:
            self.odoo = odoorpc.ODOO(self.host, protocol='jsonrpc+ssl', port=443, timeout=300)
            self.odoo.login(self.db, self.user, self.password)

    def _get_partner_by_vat(self, vat):
        if not vat: return None
        res = self.odoo.env['res.partner'].search([('vat', '=', vat)], limit=1)
        return res[0] if res else None

    def _get_move_by_ref(self, partner_id, ref, move_type='in_invoice'):
        """Búsqueda QUIRÚRGICA: Partner OBLIGATORIO + Referencia flexible."""
        if not ref or not partner_id: return None
        only_numbers = "".join(re.findall(r'\d+', str(ref))).lstrip('0')
        if not only_numbers: return None

        # Filtramos SIEMPRE por el partner específico para evitar falsos positivos de otros proveedores
        domain = [('move_type', '=', move_type), ('partner_id', '=', partner_id), ('ref', 'ilike', f"%{only_numbers}%")]
        found_moves = self.odoo.env['account.move'].search_read(domain, ['id', 'ref'])
        
        for m in found_moves:
            clean_found = "".join(re.findall(r'\d+', str(m['ref']))).lstrip('0')
            if clean_found == only_numbers: 
                return m['id']
        
        return None

    def handle_invoice_audit(self, xml_data, attachments):
        """Crea o audita facturas de compra/remitos."""
        partner_id = self._get_partner_by_vat(xml_data['rut'])
        if not partner_id: 
            print(f"❌ Partner {xml_data['rut']} ({xml_data['nombre']}) no existe en Odoo. Saltando.")
            return None
            
        inv_id = self._get_move_by_ref(partner_id, xml_data['numero_factura'], 'in_invoice')
        
        if inv_id:
            logger.info(f"🟡 Factura {xml_data['numero_factura']} ya existe (ID:{inv_id}). Verificando archivos...")
        else:
            print(f"🚀 Creando NUEVA Factura {xml_data['numero_factura']} de {xml_data['nombre']}...")
            vals = {
                'move_type': 'in_invoice',
                'partner_id': partner_id,
                'ref': xml_data['numero_factura'],
                'invoice_date': xml_data['fecha_factura'],
                'invoice_line_ids': [(0, 0, {'name': 'Importación Automática UCFE', 'price_unit': xml_data['total']})],
                'invoice_cash_rounding_id': 1 if xml_data.get('redondeo_xml_origin', 0) != 0 else False
            }
            inv_id = self.odoo.env['account.move'].create(vals)
            self.odoo.env['account.move'].action_post([inv_id])
            print(f"✅ Factura creada exitosamente (ID:{inv_id})")
            
        self._attach_files('account.move', inv_id, attachments)
        return inv_id

    def handle_resguardo(self, xml_data, attachments):
        """Maneja e-Resguardo (182): Nesta recibe retención de un Cliente."""
        partner_id = self._get_partner_by_vat(xml_data['rut'])
        if not partner_id: return None
        
        # 1. IA decide Diario (RER, REE, RET)
        prompt = f"Elige el diario de retención RECIBIDO para: {xml_data}. REE (Estado), RER (IRPF), RET (Tarjetas). Responde solo el código."
        code = self.llama_ia_text(prompt)
        journal = self.odoo.env['account.journal'].search([('code', '=', code)], limit=1)
        
        print(f"🚀 Creando Resguardo {xml_data['serie']}-{xml_data['numero']} en diario {code}...")
        pay_id = self.odoo.env['account.payment'].create({
            'partner_id': partner_id, 'amount': xml_data['total'],
            'date': xml_data['fecha_factura'], 'journal_id': journal[0] if journal else 32,
            'payment_type': 'inbound', 'partner_type': 'customer',
            'ref': f"Resguardo {xml_data['serie']}-{xml_data['numero']}"
        })
        self.odoo.env['account.payment'].action_post([pay_id])
        self._attach_files('account.payment', pay_id, attachments)
        print(f"✅ Resguardo creado (ID:{pay_id})")
        return pay_id

    def process_universal(self, attachments):
        self.conectar()
        xml_att = next((a for a in attachments if 'xml' in a['name'].lower()), None)
        if xml_att:
            docs = CFEParser.parse(base64.b64decode(xml_att['datas']))
            for doc in docs:
                tipo = str(doc.get('tipo_cfe'))
                if tipo in ['101', '111', '181']: return self.handle_invoice_audit(doc, attachments)
                elif tipo == '182': return self.handle_resguardo(doc, attachments)
        return None

    def _attach_files(self, res_model, res_id, attachments):
        if not res_id: return
        existing = [a['name'] for a in self.odoo.env['ir.attachment'].search_read([('res_model', '=', res_model), ('res_id', '=', res_id)], ['name'])]
        for att in attachments:
            if att['name'] not in existing:
                self.odoo.env['ir.attachment'].create({'name': att['name'], 'datas': att['datas'], 'res_model': res_model, 'res_id': res_id, 'type': 'binary'})

    def llama_ia_text(self, prompt):
        if not hasattr(self, 'genai_client'): return "REE"
        response = self.genai_client.models.generate_content(model=self.model_name, contents=prompt)
        return response.text.strip()

if __name__ == "__main__":
    pass
