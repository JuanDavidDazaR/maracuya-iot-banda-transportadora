/*
  ============================================================================
  Nodo sensor ESP32 - Linea de clasificacion, conteo y empaque de maracuya
  VERSION CON WIFI + MQTT (publicador y suscriptor con control de LED)
  Broker: broker.emqx.io:1883 (publico, sin usuario/password)
  ============================================================================
*/

#include <WiFi.h>
#include <PubSubClient.h>
#include <HX711.h>
#include <ArduinoJson.h>

// ---------------------------------------------------------------------------
// WIFI
// ---------------------------------------------------------------------------
const char* ssid = "";
const char* password = "";

// ---------------------------------------------------------------------------
// MQTT
// ---------------------------------------------------------------------------
const char* mqttBroker = "broker.emqx.io";
const int mqttPort = 1883;
const char* mqttClientId = "";
const char* topicData = "maracuya/banda/data";       // publicador
const char* topicCommand = "maracuya/banda/command"; // suscriptor

WiFiClient espClient;
PubSubClient client(espClient);

// ---------------------------------------------------------------------------
// IDENTIFICACION DEL NODO
// ---------------------------------------------------------------------------
const char* ID_BANDA = "";
char idProveedorSeleccionado[10] = ""; // se llena con el menu inicial

// ---------------------------------------------------------------------------
// PINES - Sensores y Actuadores
// ---------------------------------------------------------------------------
#define LED_PIN          2    // LED integrado en la mayoria de placas ESP32

#define IR_ENTRADA_PIN   16   // conteo de entrada
#define IR_BUENA_PIN     17   // conteo de fruta buena (post-rechazo)

// HX711 + celda de carga (pines y factor YA VALIDADOS con peso real)
#define HX711_DT_PIN     21
#define HX711_SCK_PIN    23
const float FACTOR_CALIBRACION = 214000.00;

// TCS3200 (color)
#define TCS_S0_PIN       18
#define TCS_S1_PIN       5
#define TCS_S2_PIN       2
#define TCS_S3_PIN       4
#define TCS_OUT_PIN      15

// ---------------------------------------------------------------------------
// PARAMETROS DE PROCESO
// ---------------------------------------------------------------------------
float pesoObjetivoKg = 10.0;
const unsigned long DEBOUNCE_MS = 50;
const unsigned long INTERVALO_LECTURA_PESO_MS = 200;
const unsigned long INTERVALO_IMPRESION_MS = 1000;

HX711 bascula;

int idCajaActual = 1;
volatile unsigned long frutasProcesadas = 0;
volatile unsigned long frutasBuenas     = 0;
unsigned long frutasRechazadas          = 0;

volatile bool nuevaFrutaEntrada = false;
volatile bool nuevaFrutaBuena   = false;
volatile unsigned long ultimoPulsoEntrada = 0;
volatile unsigned long ultimoPulsoBuena   = 0;

unsigned long ultimaLecturaPeso = 0;
unsigned long ultimaImpresion   = 0;
float pesoActualKg = 0.0;

bool cajaCompletada = false;

const unsigned long UMBRAL_OSCURO_US = 2000;
const float TOLERANCIA_RATIO = 0.15;

enum ColorFruta { ROJO, VERDE, AZUL, AMARILLO, MORADO, OSCURO };

// ---------------------------------------------------------------------------
// ISRs
// ---------------------------------------------------------------------------
void IRAM_ATTR isrEntrada() {
  unsigned long ahora = millis();
  if (ahora - ultimoPulsoEntrada > DEBOUNCE_MS) {
    nuevaFrutaEntrada = true;
    ultimoPulsoEntrada = ahora;
  }
}

void IRAM_ATTR isrBuena() {
  unsigned long ahora = millis();
  if (ahora - ultimoPulsoBuena > DEBOUNCE_MS) {
    nuevaFrutaBuena = true;
    ultimoPulsoBuena = ahora;
  }
}

// ---------------------------------------------------------------------------
// TCS3200
// ---------------------------------------------------------------------------
unsigned long leerCanalTCS(bool s2, bool s3) {
  digitalWrite(TCS_S2_PIN, s2);
  digitalWrite(TCS_S3_PIN, s3);
  return pulseIn(TCS_OUT_PIN, LOW, 25000);
}

ColorFruta leerColorTCS3200() {
  unsigned long rojo  = leerCanalTCS(LOW, LOW);
  unsigned long verde = leerCanalTCS(HIGH, HIGH);
  unsigned long azul  = leerCanalTCS(LOW, HIGH);

  if (rojo > UMBRAL_OSCURO_US && verde > UMBRAL_OSCURO_US && azul > UMBRAL_OSCURO_US) {
    return OSCURO;
  }
  if (rojo < verde * (1 - TOLERANCIA_RATIO) && rojo < azul * (1 - TOLERANCIA_RATIO)) {
    return ROJO;
  }
  if (verde < rojo * (1 - TOLERANCIA_RATIO) && verde < azul * (1 - TOLERANCIA_RATIO)) {
    return VERDE;
  }
  if (azul < rojo * (1 - TOLERANCIA_RATIO) && azul < verde * (1 - TOLERANCIA_RATIO)) {
    return AZUL;
  }
  if (rojo < verde && azul < verde) {
    return AMARILLO;
  }
  if (rojo < verde && verde < azul) {
    return MORADO;
  }
  return OSCURO;
}

bool esFrutaBuena(ColorFruta color) {
  if (color == OSCURO) return false;
  return true;
}

const char* nombreColor(ColorFruta color) {
  switch (color) {
    case ROJO:     return "ROJO";
    case VERDE:    return "VERDE";
    case AZUL:     return "AZUL";
    case AMARILLO: return "AMARILLO";
    case MORADO:   return "MORADO";
    case OSCURO:   return "OSCURO";
    default:       return "OSCURO";
  }
}

// ---------------------------------------------------------------------------
// Tiempo transcurrido
// ---------------------------------------------------------------------------
String tiempoTranscurrido() {
  unsigned long s = millis() / 1000;
  unsigned int horas = s / 3600;
  unsigned int minutos = (s % 3600) / 60;
  unsigned int segundos = s % 60;
  char buffer[9];
  snprintf(buffer, sizeof(buffer), "%02u:%02u:%02u", horas, minutos, segundos);
  return String(buffer);
}

// ---------------------------------------------------------------------------
// WIFI
// ---------------------------------------------------------------------------
void setup_wifi() {
  delay(10);
  Serial.println();
  Serial.print("Connecting to ");
  Serial.println(ssid);
  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("");
  Serial.println("WiFi connected");
  Serial.print("IP address: ");
  Serial.println(WiFi.localIP());
}

// ---------------------------------------------------------------------------
// MQTT - Callback (Procesamiento de comandos JSON)
// ---------------------------------------------------------------------------
void callback(char* topic, byte* payload, unsigned int length) {
  Serial.print("Mensaje recibido en [");
  Serial.print(topic);
  Serial.print("]: ");

  // Deserialización directa del payload recibido usando ArduinoJson
  JsonDocument doc;
  DeserializationError error = deserializeJson(doc, payload, length);

  if (error) {
    Serial.print("Error al parsear JSON: ");
    Serial.println(error.c_str());
    return;
  }

  // Verificamos si la llave "led" está presente en el JSON recibido
  if (doc.containsKey("led")) {
    int estadoLed = doc["led"];
    if (estadoLed == 1) {
      digitalWrite(LED_PIN, HIGH);
      Serial.println("-> Comando ejecutado: LED ENCENDIDO");
    } else if (estadoLed == 0) {
      digitalWrite(LED_PIN, LOW);
      Serial.println("-> Comando ejecutado: LED APAGADO");
    }
  }
}

void reconnectMQTT() {
  while (!client.connected()) {
    Serial.print("Conectando a MQTT (");
    Serial.print(mqttBroker);
    Serial.print(")...");
    if (client.connect(mqttClientId)) { // broker publico, sin usuario/password
      Serial.println("conectado");
      client.subscribe(topicCommand);
      Serial.print("Suscrito a: ");
      Serial.println(topicCommand);
    } else {
      Serial.print("fallo, rc=");
      Serial.print(client.state());
      Serial.println(" reintentando en 5s");
      delay(5000);
    }
  }
}

// ---------------------------------------------------------------------------
// JSON + publicacion MQTT
// ---------------------------------------------------------------------------
void imprimirYPublicarEstado(const char* estado) {
  JsonDocument doc;
  doc["idBanda"] = ID_BANDA;
  doc["idProveedor"] = idProveedorSeleccionado;
  doc["idCaja"] = idCajaActual;
  doc["fechaHora"] = tiempoTranscurrido();
  doc["frutasProcesadas"] = frutasProcesadas;
  doc["frutasBuenas"] = frutasBuenas;
  doc["frutasRechazadas"] = frutasRechazadas;
  doc["pesoFinal"] = round(pesoActualKg * 100) / 100.0;
  doc["estadoCaja"] = estado;

  char buffer[256];
  size_t n = serializeJson(doc, buffer);

  Serial.println(buffer);
  client.publish(topicData, buffer, n);
}

void iniciarNuevaCaja() {
  idCajaActual++;
  frutasProcesadas = 0;
  frutasBuenas = 0;
  frutasRechazadas = 0;
  bascula.tare();
  pesoActualKg = 0.0;
  cajaCompletada = false;
}

// ---------------------------------------------------------------------------
// Menu de proveedor y espera de "start"
// ---------------------------------------------------------------------------
void elegirProveedor() {
  Serial.println("Selecciona el proveedor de la fruta para esta simulacion:");
  Serial.println("  1. Roldanillo (PROV-001)");
  Serial.println("  2. La Union (PROV-002)");
  Serial.println("  3. El Dovio (PROV-003)");
  Serial.println("  4. Toro (PROV-004)");

  while (true) {
    Serial.print("Proveedor [1-4]: ");
    while (!Serial.available()) delay(10);
    String opcion = Serial.readStringUntil('\n');
    opcion.trim();

    if (opcion == "1") { strcpy(idProveedorSeleccionado, "PROV-001"); break; }
    if (opcion == "2") { strcpy(idProveedorSeleccionado, "PROV-002"); break; }
    if (opcion == "3") { strcpy(idProveedorSeleccionado, "PROV-003"); break; }
    if (opcion == "4") { strcpy(idProveedorSeleccionado, "PROV-004"); break; }

    Serial.println("Opcion invalida. Ingresa un numero entre 1 y 4.");
  }

  Serial.print("Proveedor seleccionado: ");
  Serial.println(idProveedorSeleccionado);
  Serial.println();
}

void esperarComandoStart() {
  while (true) {
    Serial.print("Escribe 'start' y presiona Enter para iniciar: ");
    while (!Serial.available()) delay(10);
    String comando = Serial.readStringUntil('\n');
    comando.trim();
    comando.toLowerCase();

    if (comando == "start") {
      Serial.println();
      return;
    }
    Serial.println("Comando no reconocido. Escribe 'start' para comenzar.");
  }
}

// ---------------------------------------------------------------------------
// SETUP
// ---------------------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  delay(500);

  // --- LED Integrado ---
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW); // Inicia apagado

  // --- IR (FC-51) ---
  pinMode(IR_ENTRADA_PIN, INPUT);
  pinMode(IR_BUENA_PIN, INPUT);
  attachInterrupt(digitalPinToInterrupt(IR_ENTRADA_PIN), isrEntrada, FALLING);
  attachInterrupt(digitalPinToInterrupt(IR_BUENA_PIN), isrBuena, FALLING);

  // --- TCS3200 ---
  pinMode(TCS_S0_PIN, OUTPUT);
  pinMode(TCS_S1_PIN, OUTPUT);
  pinMode(TCS_S2_PIN, OUTPUT);
  pinMode(TCS_S3_PIN, OUTPUT);
  pinMode(TCS_OUT_PIN, INPUT);
  digitalWrite(TCS_S0_PIN, HIGH);
  digitalWrite(TCS_S1_PIN, LOW);

  // --- HX711 (pines y factor ya validados con peso real) ---
  bascula.begin(HX711_DT_PIN, HX711_SCK_PIN);
  bascula.set_scale(FACTOR_CALIBRACION);
  bascula.tare();

  // --- WiFi + MQTT ---
  setup_wifi();
  client.setServer(mqttBroker, mqttPort);
  client.setCallback(callback);

  // --- Flujo interactivo, igual que el simulador Python ---
  elegirProveedor();
  esperarComandoStart();

  Serial.println("Nodo " + String(ID_BANDA) + " listo. Proveedor: " + String(idProveedorSeleccionado) +
                 " | Peso objetivo: " + String(pesoObjetivoKg) + " kg");
  Serial.println("Publicador: " + String(topicData));
  Serial.println("Suscriptor: " + String(topicCommand));
}

// ---------------------------------------------------------------------------
// LOOP
// ---------------------------------------------------------------------------
void loop() {
  if (!client.connected()) {
    reconnectMQTT();
  }
  client.loop();

  if (nuevaFrutaEntrada) {
    nuevaFrutaEntrada = false;

    ColorFruta color = leerColorTCS3200();
    Serial.print("[debug color, no se envia] ");
    Serial.println(nombreColor(color));

    if (!esFrutaBuena(color)) {
      frutasRechazadas++;
      frutasProcesadas++;
    }
  }

  if (nuevaFrutaBuena) {
    nuevaFrutaBuena = false;
    frutasBuenas++;
    frutasProcesadas++;
  }

  unsigned long ahora = millis();
  if (ahora - ultimaLecturaPeso >= INTERVALO_LECTURA_PESO_MS) {
    ultimaLecturaPeso = ahora;
    if (bascula.is_ready()) {
      pesoActualKg = bascula.get_units(1);
      if (pesoActualKg < 0) pesoActualKg = 0;
    }
  }

  if (ahora - ultimaImpresion >= INTERVALO_IMPRESION_MS) {
    ultimaImpresion = ahora;
    imprimirYPublicarEstado(cajaCompletada ? "Completa" : "En proceso");
  }

  if (!cajaCompletada && pesoActualKg >= pesoObjetivoKg) {
    cajaCompletada = true;
    imprimirYPublicarEstado("Completa");
    iniciarNuevaCaja();
  }
}