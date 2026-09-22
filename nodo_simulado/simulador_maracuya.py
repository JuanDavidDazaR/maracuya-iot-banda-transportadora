import json
import time
import random
import argparse
from datetime import datetime, timezone
import paho.mqtt.client as mqtt

# Configuración MQTT requerida
BROKER = "broker.emqx.io"
PORT = 1883
TOPIC = "maracuya/banda/data"
CLIENT_ID = "cali-banda01"

PESO_MIN_FRUTA, PESO_MAX_FRUTA = 80.0, 130.0
INTERVALO_MIN_FRUTA, INTERVALO_MAX_FRUTA = 0.3, 1.2

PROVEEDORES = {
    "1": ("PROV-001", "Roldanillo"),
    "2": ("PROV-002", "La Unión"),
    "3": ("PROV-003", "El Dovio"),
    "4": ("PROV-004", "Toro"),
}


def elegir_proveedor() -> str:
    """
    Muestra el menú de proveedores (1 a 4) y pide al usuario que elija
    uno antes de iniciar la simulación. Devuelve el idProveedor elegido.
    """
    print("Selecciona el proveedor de la fruta para esta simulación:")
    for numero, (id_prov, nombre) in PROVEEDORES.items():
        print(f"  {numero}. {nombre} ({id_prov})")

    while True:
        opcion = input("Proveedor [1-4]: ").strip()
        if opcion in PROVEEDORES:
            id_prov, nombre = PROVEEDORES[opcion]
            print(f"Proveedor seleccionado: {nombre} ({id_prov})\n")
            return id_prov
        print("Opción inválida. Ingresa un número entre 1 y 4.")


def esperar_comando_start() -> None:
    """
    Bloquea la ejecución hasta que el usuario escriba 'start' y presione
    Enter, para dar tiempo a revisar la configuración antes de que
    empiecen a enviarse los mensajes JSON.
    """
    while True:
        comando = input("Escribe 'start' y presiona Enter para iniciar la simulación: ").strip().lower()
        if comando == "start":
            print()
            return
        print("Comando no reconocido. Escribe 'start' para comenzar.")


class EstadisticasLinea:
    """
    Mantiene el promedio de tiempo de llenado de caja a lo largo de la
    simulación. Empieza con un valor por defecto (no hay historial aún)
    y se va recalculando como promedio simple de los tiempos reales de
    cada caja completada.
    """

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

    while peso_acumulado_g < peso_objetivo_g:
        time.sleep(random.uniform(INTERVALO_MIN_FRUTA, INTERVALO_MAX_FRUTA))
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
                "frutasBuenas": frutas_buenas,
                "frutasRechazadas": frutas_rechazadas,
                "pesoActual": round(peso_acumulado_g / 1000, 2),
                "pesoObjetivo": peso_objetivo_kg,
            }
            alerta_enviada = True

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

    while True:
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
    """Convierte el diccionario a JSON y lo publica en el tópico MQTT especificado."""
    json_data = json.dumps(payload, ensure_ascii=False)
    client.publish(TOPIC, json_data, qos=1)
    # Mostramos en consola lo que se acaba de enviar
    print(f"[MQTT -> {TOPIC}] {json_data}")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Simula el nodo sensor (ESP32) de la banda transportadora de "
            "maracuyá y publica un registro JSON en el broker MQTT cada vez "
            "que se completa una caja o se genera una alerta."
        )
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
    parser.add_argument("--id-banda", default="cali-banda01")
    parser.add_argument("--id-proveedor", default=None)
    args = parser.parse_args()

    id_proveedor = args.id_proveedor if args.id_proveedor else elegir_proveedor()
    esperar_comando_start()

    # --- Configuración y Conexión al Broker MQTT ---
    # Se utiliza CallbackAPIVersion.VERSION2 si usas paho-mqtt >= 2.0
    try:
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, 
            client_id=CLIENT_ID
        )
    except AttributeError:
        # Compatibilidad con versiones antiguas de paho-mqtt (< 2.0)
        client = mqtt.Client(client_id=CLIENT_ID)

    print(f"Conectando al broker MQTT {BROKER}:{PORT} con Client ID: '{CLIENT_ID}'...")
    try:
        client.connect(BROKER, PORT, keepalive=60)
        client.loop_start()  # Inicia hilo en segundo plano para gestionar reconexiones
        print("Conectado exitosamente al broker MQTT.\n")
    except Exception as e:
        print(f"Error al conectar con el broker MQTT: {e}")
        return

    estadisticas = EstadisticasLinea(promedio_inicial_seg=args.promedio_inicial_min * 60)

    print(
        f"Nodo {args.id_banda}: simulando banda transportadora, "
        f"proveedor = {id_proveedor}, "
        f"peso objetivo por caja = {args.peso_objetivo} kg, "
        f"prob. fruta buena = {args.prob_buena}. "
        f"Presiona Ctrl+C para detener.\n"
    )

    id_caja = args.caja_inicial
    cajas_generadas = 0

    try:
        while args.num_cajas is None or cajas_generadas < args.num_cajas:
            ultimo_mensaje = None
            for mensaje in generar_caja(
                id_caja, args.peso_objetivo, args.prob_buena,
                args.id_banda, id_proveedor,
                estadisticas, args.factor_alerta,
            ):
                # Publicar evento/alerta vía MQTT
                enviar_por_mqtt(client, mensaje)
                ultimo_mensaje = mensaje

            if ultimo_mensaje and ultimo_mensaje.get("estadoCaja") == "Completa":
                for evento in simular_cambio_caja(
                    id_caja, args.id_banda, id_proveedor,
                    args.tiempo_cambio_min_seg, args.tiempo_cambio_max_seg,
                    args.prob_operario_lento,
                    args.tiempo_cambio_lento_min_seg, args.tiempo_cambio_lento_max_seg,
                    args.umbral_banda_detenida_seg,
                ):
                    # Publicar alerta de banda detenida vía MQTT
                    enviar_por_mqtt(client, evento)

            id_caja += 1
            cajas_generadas += 1
    except KeyboardInterrupt:
        print("\nSimulación detenida por el usuario.")
    finally:
        client.loop_stop()
        client.disconnect()
        print("Conexión MQTT cerrada.")


if __name__ == "__main__":
    main()