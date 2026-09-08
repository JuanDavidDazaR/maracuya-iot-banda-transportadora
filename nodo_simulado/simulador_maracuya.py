
import json
import time
import random
import argparse
from datetime import datetime, timezone

# Peso simulado por fruta buena depositada en la caja (gramos)
PESO_MIN_FRUTA, PESO_MAX_FRUTA = 80.0, 130.0

# Tiempo simulado entre el paso de dos frutas consecutivas por el sensor
# de conteo de entrada (segundos)
INTERVALO_MIN_FRUTA, INTERVALO_MAX_FRUTA = 0.3, 1.2


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
    """
    Simula el llenado de una caja fruto a fruto hasta alcanzar el peso
    objetivo. Es un generador que cede como máximo dos mensajes:

      1. (Opcional, una sola vez) "alertaCajaNoLlenada": si pasa el
         tiempo esperado sin completarse la caja (ej. porque llega
         fruta muy dañada y el rechazo automático descarta casi todo).
         El conteo sigue en segundo plano después de esta alerta; no
         se repite.
      2. "cajaCompletada": el registro final, cuando la caja alcanza el
         peso objetivo. Este es el único mensaje del camino normal
         (sin problemas).
    """
    peso_objetivo_g = peso_objetivo_kg * 1000
    frutas_procesadas = 0
    frutas_buenas = 0
    frutas_rechazadas = 0
    peso_acumulado_g = 0.0

    tiempo_inicio = time.monotonic()
    alerta_enviada = False

    while peso_acumulado_g < peso_objetivo_g:
        # Sensor de conteo de entrada: pasa una nueva fruta por la banda
        time.sleep(random.uniform(INTERVALO_MIN_FRUTA, INTERVALO_MAX_FRUTA))
        frutas_procesadas += 1

        # Sensor de color: clasifica la fruta
        es_buena = random.random() < prob_fruta_buena

        if es_buena:
            # Sensor de conteo de fruta buena + celda de carga
            frutas_buenas += 1
            peso_acumulado_g += random.uniform(PESO_MIN_FRUTA, PESO_MAX_FRUTA)
        else:
            # Actuador expulsor retira la fruta antes del empaque
            frutas_rechazadas += 1

        # Umbral dinámico: factor_alerta veces el promedio de llenado actual
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

    # El tiempo de llenado se mide internamente con el reloj del ESP32
    # (time.monotonic() aquí simula millis()) para alimentar el promedio
    # y la lógica de alertas, pero NO se envía en el JSON: la plataforma
    # puede calcularlo restando marcas de tiempo entre mensajes. El
    # porcentaje de rechazo tampoco se envía: se calcula en la plataforma
    # como frutasRechazadas / frutasProcesadas.
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
    """
    Simula el tiempo que tarda el operario en retirar la caja llena y
    reanudar la banda transportadora. Si el cambio se hace dentro del
    tiempo esperado (camino normal), esta función no emite NADA. Solo
    si se supera "umbral_alerta_seg" sin que se haya cambiado la caja,
    cede UNA sola vez "alertaCajaNoCambiada" (no se repite).
    """
    # Con cierta probabilidad, el operario se demora mucho en atender la línea
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


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Simula el nodo sensor de la banda transportadora de "
            "maracuyá y publica un registro JSON cada vez que se completa una caja, "
            "además de alertas si una caja tarda demasiado en llenarse."
        )
    )
    parser.add_argument(
        "-p", "--peso-objetivo", type=float, default=10.0,
        help="Peso objetivo de la caja en kg (por defecto: 10.0)."
    )
    parser.add_argument(
        "-c", "--caja-inicial", type=int, default=1,
        help="Número de caja con el que inicia el conteo (por defecto: 1)."
    )
    parser.add_argument(
        "-n", "--num-cajas", type=int, default=None,
        help="Número de cajas a simular. Si no se indica, simula indefinidamente."
    )
    parser.add_argument(
        "-b", "--prob-buena", type=float, default=0.85,
        help=(
            "Probabilidad (0 a 1) de que una fruta sea clasificada como buena. "
            "Bájala (ej. 0.1) para simular fruta muy dañada y forzar que la caja "
            "se estanque y se disparen alertas (por defecto: 0.85)."
        )
    )
    parser.add_argument(
        "--promedio-inicial-min", type=float, default=2.0,
        help=(
            "Promedio de llenado de caja asumido ANTES de tener historial real, "
            "en minutos (por defecto: 2.0). Una vez se completan cajas reales, el "
            "promedio se recalcula solo."
        )
    )
    parser.add_argument(
        "--factor-alerta", type=float, default=2.0,
        help=(
            "Múltiplo del promedio de llenado a partir del cual se considera que "
            "una caja está estancada y se envía la alerta (por defecto: 2.0, ej. "
            "si el promedio es 5 min, alerta a los 10 min)."
        )
    )
    parser.add_argument(
        "--tiempo-cambio-min-seg", type=float, default=2.0,
        help="Tiempo mínimo (seg) que tarda el operario en cambiar la caja llena en condiciones normales (por defecto: 2.0)."
    )
    parser.add_argument(
        "--tiempo-cambio-max-seg", type=float, default=6.0,
        help="Tiempo máximo (seg) que tarda el operario en cambiar la caja llena en condiciones normales (por defecto: 6.0)."
    )
    parser.add_argument(
        "--prob-operario-lento", type=float, default=0.15,
        help=(
            "Probabilidad (0 a 1) de que el operario se demore mucho en cambiar "
            "la caja (distraído, atendiendo otra tarea), simulando que la banda "
            "queda detenida sin acción (por defecto: 0.15)."
        )
    )
    parser.add_argument(
        "--tiempo-cambio-lento-min-seg", type=float, default=30.0,
        help="Tiempo mínimo (seg) de demora del operario en el caso 'lento' (por defecto: 30.0)."
    )
    parser.add_argument(
        "--tiempo-cambio-lento-max-seg", type=float, default=90.0,
        help="Tiempo máximo (seg) de demora del operario en el caso 'lento' (por defecto: 90.0)."
    )
    parser.add_argument(
        "--umbral-banda-detenida-seg", type=float, default=15.0,
        help=(
            "Segundos que la banda puede estar detenida sin acción del operario "
            "antes de enviar (y repetir) la alerta 'alertaBandaDetenida' al "
            "dashboard del supervisor (por defecto: 15.0)."
        )
    )
    parser.add_argument(
        "--id-banda", default="cali-banda01",
        help="Identificador de la banda transportadora (por defecto: cali.banda01)."
    )
    parser.add_argument(
        "--id-proveedor", default="PROV-001",
        help="Identificador del proveedor/lote de fruta (por defecto: PROV-001)."
    )
    args = parser.parse_args()

    estadisticas = EstadisticasLinea(promedio_inicial_seg=args.promedio_inicial_min * 60)

    print(
        f"Nodo {args.id_banda}: simulando banda transportadora, "
        f"proveedor = {args.id_proveedor}, "
        f"peso objetivo por caja = {args.peso_objetivo} kg, "
        f"prob. fruta buena = {args.prob_buena}, "
        f"promedio inicial = {args.promedio_inicial_min} min, "
        f"factor de alerta = {args.factor_alerta}x. "
        f"Presiona Ctrl+C para detener.\n"
    )

    id_caja = args.caja_inicial
    cajas_generadas = 0

    try:
        while args.num_cajas is None or cajas_generadas < args.num_cajas:
            ultimo_mensaje = None
            for mensaje in generar_caja(
                id_caja, args.peso_objetivo, args.prob_buena,
                args.id_banda, args.id_proveedor,
                estadisticas, args.factor_alerta,
            ):
                # Un objeto JSON por línea, publicado en el momento en que ocurre
                print(json.dumps(mensaje, ensure_ascii=False))
                ultimo_mensaje = mensaje

            # Si la caja se completó, la banda se detiene (motor apagado) hasta
            # que el operario la cambie; mientras tanto puede haber alertas.
            if ultimo_mensaje and ultimo_mensaje.get("estadoCaja") == "Completa":
                for evento in simular_cambio_caja(
                    id_caja, args.id_banda, args.id_proveedor,
                    args.tiempo_cambio_min_seg, args.tiempo_cambio_max_seg,
                    args.prob_operario_lento,
                    args.tiempo_cambio_lento_min_seg, args.tiempo_cambio_lento_max_seg,
                    args.umbral_banda_detenida_seg,
                ):
                    print(json.dumps(evento, ensure_ascii=False))

            id_caja += 1
            cajas_generadas += 1
    except KeyboardInterrupt:
        print("\nSimulación detenida.")


if __name__ == "__main__":
    main()
