import time
import logging
from dispatcher import DispatcherNesta

# ==============================================================
# ENTRYPOINT PARA DOCKER (QNAP CONTAINER STATION)
# ==============================================================
# Este script mantiene vivo el contenedor y ejecuta el dispatcher 
# periódicamente según el intervalo definido.

# Intervalo en minutos entre cada ciclo
INTERVALO_MINUTOS = 5

if __name__ == "__main__":
    logging.info(f"🚀 Iniciando Nesta AI Daemon en modo continuo. Intervalo: {INTERVALO_MINUTOS} min.")
    dispatcher = DispatcherNesta()
    
    while True:
        try:
            dispatcher.ejecutar_ciclo_completo()
        except Exception as e:
            logging.error(f"❌ Error crítico no manejado en el ciclo principal: {e}")
        
        logging.info(f"💤 Ciclo finalizado. Durmiendo por {INTERVALO_MINUTOS} minutos...")
        time.sleep(INTERVALO_MINUTOS * 60)
