import json
import time
import random
import argparse
import threading
from datetime import datetime, timezone
import paho.mqtt.client as mqtt

# Configuración MQTT
BROKER = "broker.emqx.io"
PORT = 1883
TOPIC_PUB = "maracuya/banda/data"
TOPIC_SUB = "maracuya/banda/cali-banda01/command" # cambia cali-banda01 por el ID_BANDA correspondiente
CLIENT_ID = "cali-banda01" # cambia cali-banda01 por el ID_BANDA correspondiente

PESO_MIN_FRUTA, PESO_MAX_FRUTA = 80.0, 130.0
INTERVALO_MIN_FRUTA, INTERVALO_MAX_FRUTA = 0.3, 1.2

PROVEEDORES = {
    "1": ("PROV-001", "Roldanillo"),
    "2": ("PROV-002", "La Unión"),
    "3": ("PROV-003", "El Dovio"),
    "4": ("PROV-004", "Toro"),
}

# --- Variables de control de estado global ---
simulacion_activa = False
id_proveedor_actual = "PROV-001"
evento_start = threading.Event()


class EstadisticasLinea:
    def __init__(self, promedio_inicial_seg: float):
        self.promedio_inicial_seg = promedio_inicial_seg
        self.tiempos_llenado_seg = []

    @property
    def promedio_actual_seg(self) -> float:
        if not self.tiempos_llenado_seg:
            return self.promedio_inicial_seg
        return sum(self.tiempos_llenado_seg) / len(self.tiempos_llenado_seg)

    @property
    def num_cajas_en_historial(self) -> int:
        return len(self.tiempos_llenado_seg)

    def registrar_tiempo(self, tiempo_seg: float) -> None:
        self.tiempos_llenado_seg.append(tiempo_seg)


def generar_caja(id_caja: int, peso_objetivo_kg: float, prob_fruta_buena: float,
                 id_banda: str, id_proveedor: str,
                 estadisticas: EstadisticasLinea, factor_alerta: float):
    peso_objetivo_g = peso_objetivo_kg * 1000
    frutas_procesadas = 0
    frutas_buenas = 0
    frutas_rechazadas = 0
    peso_acumulado_g = 0.0

    tiempo_inicio = time.monotonic()
    alerta_enviada = False

    while peso_acumulado_g < peso_objetivo_g and simulacion_activa:
        time.sleep(random.uniform(INTERVALO_MIN_FRUTA, INTERVALO_MAX_FRUTA))
        
        if not simulacion_activa:
            break

        frutas_procesadas += 1
        es_buena = random.random() < prob_fruta_buena

        if es_buena:
            frutas_buenas += 1
            peso_acumulado_g += random.uniform(PESO_MIN_FRUTA, PESO_MAX_FRUTA)
        else:
            frutas_rechazadas += 1

        promedio_seg = estadisticas.promedio_actual_seg
        umbral_seg = promedio_seg * factor_alerta
        tiempo_transcurrido = time.monotonic() - tiempo_inicio

        if (not alerta_enviada and peso_acumulado_g < peso_objetivo_g
                and tiempo_transcurrido >= umbral_seg):
            yield {
                "idBanda": id_banda,
                "idProveedor": id_proveedor,
                "idCaja": id_caja,
                "fechaHora": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "motivo": "tiempoExcedidoSinCompletar",
                "tiempoTranscurridoSeg": round(tiempo_transcurrido, 1),
                "promedioLlenadoActualSeg": round(promedio_seg, 1),
                "cajasEnHistorialPromedio": estadisticas.num_cajas_en_historial,
                "frutasProcesadas": frutas_procesadas,
                "frutasBuenas": frutas_buenas,  # CORREGIDO: antes decía frutasBuenas
                "frutasRechazadas": frutas_rechazadas,
                "pesoActual": round(peso_acumulado_g / 1000, 2),
                "pesoObjetivo": peso_objetivo_kg,
            }
            alerta_enviada = True

    if simulacion_activa:
        tiempo_total_seg = time.monotonic() - tiempo_inicio
        estadisticas.registrar_tiempo(tiempo_total_seg)

        yield {
            "idBanda": id_banda,
            "idProveedor": id_proveedor,
            "idCaja": id_caja,
            "fechaHora": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "frutasProcesadas": frutas_procesadas,
            "frutasBuenas": frutas_buenas,
            "frutasRechazadas": frutas_rechazadas,
            "pesoFinal": round(peso_acumulado_g / 1000, 2),
            "estadoCaja": "Completa",
        }

def simular_cambio_caja(id_caja_completada: int, id_banda: str, id_proveedor: str,
                        tiempo_min_seg: float, tiempo_max_seg: float,
                        prob_operario_lento: float,
                        tiempo_lento_min_seg: float, tiempo_lento_max_seg: float,
                        umbral_alerta_seg: float):
    if random.random() < prob_operario_lento:
        tiempo_espera_seg = random.uniform(tiempo_lento_min_seg, tiempo_lento_max_seg)
    else:
        tiempo_espera_seg = random.uniform(tiempo_min_seg, tiempo_max_seg)

    inicio = time.monotonic()
    alerta_enviada = False
    paso_seg = 0.5

    while simulacion_activa:
        transcurrido = time.monotonic() - inicio
        if transcurrido >= tiempo_espera_seg:
            break

        time.sleep(min(paso_seg, tiempo_espera_seg - transcurrido))
        transcurrido = time.monotonic() - inicio

        if not alerta_enviada and transcurrido >= umbral_alerta_seg:
            yield {
                "idBanda": id_banda,
                "idProveedor": id_proveedor,
                "idCaja": id_caja_completada,
                "fechaHora": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "motivo": "cajaNoCambiadaPorOperario",
                "tiempoDetenidaSeg": round(transcurrido, 1),
                "umbralAlertaSeg": umbral_alerta_seg,
            }
            alerta_enviada = True


def enviar_por_mqtt(client: mqtt.Client, payload: dict) -> None:
    json_data = json.dumps(payload, ensure_ascii=False)
    client.publish(TOPIC_PUB, json_data, qos=1)
    print(f"[MQTT PUB -> {TOPIC_PUB}] {json_data}")


def on_message(client, userdata, msg):
    """Callback invocado al recibir un JSON en el tópico suscrito."""
    global simulacion_activa, id_proveedor_actual
    try:
        payload = json.loads(msg.payload.decode('utf-8'))
        print(f"\n[MQTT SUB <- {msg.topic}] Comando recibido: {payload}")

        comando = str(payload.get("command", "")).lower()

        if comando == "start":
            id_prov = payload.get("idProveedor", id_proveedor_actual)
            id_proveedor_actual = id_prov
            simulacion_activa = True
            evento_start.set()
            print(f"-> ACTUACIÓN: Simulación INICIADA. Proveedor: {id_proveedor_actual}\n")

        elif comando == "stop":
            simulacion_activa = False
            evento_start.clear()
            print("-> ACTUACIÓN: Simulación DETENIDA por comando MQTT.\n")

    except json.JSONDecodeError:
        print("Error: El mensaje recibido no es un JSON válido.")
    except Exception as e:
        print(f"Error procesando el comando: {e}")


def main():
    global id_proveedor_actual, simulacion_activa

    parser = argparse.ArgumentParser(
        description="Nodo simulado con control remoto vía MQTT."
    )
    parser.add_argument("-p", "--peso-objetivo", type=float, default=10.0)
    parser.add_argument("-c", "--caja-inicial", type=int, default=1)
    parser.add_argument("-n", "--num-cajas", type=int, default=None)
    parser.add_argument("-b", "--prob-buena", type=float, default=0.85)
    parser.add_argument("--promedio-inicial-min", type=float, default=0.5)
    parser.add_argument("--factor-alerta", type=float, default=2.0)
    parser.add_argument("--tiempo-cambio-min-seg", type=float, default=2.0)
    parser.add_argument("--tiempo-cambio-max-seg", type=float, default=6.0)
    parser.add_argument("--prob-operario-lento", type=float, default=0.15)
    parser.add_argument("--tiempo-cambio-lento-min-seg", type=float, default=30.0)
    parser.add_argument("--tiempo-cambio-lento-max-seg", type=float, default=90.0)
    parser.add_argument("--umbral-banda-detenida-seg", type=float, default=15.0)
    parser.add_argument("--id-banda", default="cali-banda01") # cambia cali-banda01 por el ID_BANDA correspondiente
    parser.add_argument("--id-proveedor", default="PROV-001")
    args = parser.parse_args()

    id_proveedor_actual = args.id_proveedor

    # --- Configuración del Cliente MQTT ---
    try:
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, 
            client_id=CLIENT_ID
        )
    except AttributeError:
        client = mqtt.Client(client_id=CLIENT_ID)

    client.on_message = on_message

    print(f"Conectando al broker {BROKER}:{PORT}...")
    try:
        client.connect(BROKER, PORT, keepalive=60)
        client.subscribe(TOPIC_SUB)
        client.loop_start()
        print(f"Conectado y suscrito exitosamente a: '{TOPIC_SUB}'")
    except Exception as e:
        print(f"Error al conectar con el broker: {e}")
        return

    estadisticas = EstadisticasLinea(promedio_inicial_seg=args.promedio_inicial_min * 60)

    print("\n=======================================================")
    print(f" Esperando comando MQTT en: {TOPIC_SUB}")
    print(f" Ejemplo JSON para iniciar: {{\"command\": \"start\", \"idProveedor\": \"PROV-001\"}}")
    print(f" Ejemplo JSON para detener: {{\"command\": \"stop\"}}")
    print("=======================================================\n")

    id_caja = args.caja_inicial
    cajas_generadas = 0

    try:
        while True:
            # Espera bloqueante hasta recibir comando 'start' vía MQTT
            if not simulacion_activa:
                evento_start.wait()

            while simulacion_activa and (args.num_cajas is None or cajas_generadas < args.num_cajas):
                ultimo_mensaje = None
                for mensaje in generar_caja(
                    id_caja, args.peso_objetivo, args.prob_buena,
                    args.id_banda, id_proveedor_actual,
                    estadisticas, args.factor_alerta,
                ):
                    if not simulacion_activa:
                        break
                    enviar_por_mqtt(client, mensaje)
                    ultimo_mensaje = mensaje

                if (simulacion_activa and ultimo_mensaje 
                        and ultimo_mensaje.get("estadoCaja") == "Completa"):
                    for evento in simular_cambio_caja(
                        id_caja, args.id_banda, id_proveedor_actual,
                        args.tiempo_cambio_min_seg, args.tiempo_cambio_max_seg,
                        args.prob_operario_lento,
                        args.tiempo_cambio_lento_min_seg, args.tiempo_cambio_lento_max_seg,
                        args.umbral_banda_detenida_seg,
                    ):
                        if not simulacion_activa:
                            break
                        enviar_por_mqtt(client, evento)

                if simulacion_activa:
                    id_caja += 1
                    cajas_generadas += 1

            if simulacion_activa:
                print("Simulación completada por límite de cajas.")
                simulacion_activa = False
                evento_start.clear()

    except KeyboardInterrupt:
        print("\nPrograma finalizado manualmente.")
    finally:
        client.loop_stop()
        client.disconnect()
        print("Conexión MQTT cerrada.")


if __name__ == "__main__":
    main()