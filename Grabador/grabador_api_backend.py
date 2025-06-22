import threading
import time
import pickle
import os
import platform
import ctypes
from io import BytesIO
from flask import Flask, request, jsonify
from pynput import mouse, keyboard as pynput_keyboard
from pynput.mouse import Controller as MouseController, Button
from pynput.keyboard import Controller as KeyboardController, Key
import json
from concurrent.futures import ThreadPoolExecutor
import queue
import asyncio
import websockets

app = Flask(__name__)
from flask_cors import CORS
CORS(app, resources={
    r"/*": {
        "origins": "*",
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization"]
    }
})

# Pool de threads para operaciones no bloqueantes
thread_pool = ThreadPoolExecutor(max_workers=4)

# Global variables con thread locks optimizados
eventos = []
grabando = False
grabacion_en_memoria = BytesIO()
ml = None
kl = None
tiempo_inicio = 0.0
velocidad_reproduccion = 1.0
reproductor = None
config_grabacion = {
    "mouseMove": True,
    "mouseClick": True,
    "keyboard": True,
    "smartCapture": False,
    "fps": 60,
    "compression": "Media",
    "hotkey": "F9",
    "startDelay": 0
}

# Usar RLock para mejor rendimiento en operaciones anidadas
lock = threading.RLock()

# Cache para el estado del sistema
estado_cache = {
    "grabando": False,
    "reproduciendo": False,
    "tiene_grabacion": False,
    "velocidad": 1.0,
    "duracion": 0.0,
    "acciones": 0,
    "tamano": 0,
    "fps": 0.0
}
estado_cache_lock = threading.Lock()
estado_cache_timestamp = 0

# Cola de eventos para procesamiento asíncrono
eventos_queue = queue.Queue(maxsize=10000)

# --- WebSocket server setup ---
ws_clients = set()
ws_loop = None

async def _ws_handler(websocket):
    ws_clients.add(websocket)
    try:
        await websocket.wait_closed()
    finally:
        ws_clients.discard(websocket)

def _start_ws_server():
    """Run the WebSocket server in its own asyncio loop."""
    global ws_loop

    ws_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(ws_loop)

    try:
        async def setup_server():
            server = await websockets.serve(_ws_handler, "127.0.0.1", 8765)
            print("WebSocket servidor en ws://127.0.0.1:8765")
            return server

        ws_loop.run_until_complete(setup_server())
        ws_loop.run_forever()
    except Exception as e:
        print(f"Error en servidor WebSocket: {e}")
    finally:
        try:
            ws_loop.close()
        except Exception:
            pass

ws_thread = threading.Thread(target=_start_ws_server, daemon=True)
ws_thread.start()

async def _ws_broadcast(msg):
    """Send a message to all connected clients if recording."""
    if not grabando or not ws_clients:
        return

    desconectados = []
    payload = json.dumps(msg)
    for ws in list(ws_clients):
        try:
            await ws.send(payload)
        except websockets.ConnectionClosed:
            desconectados.append(ws)
        except Exception:
            desconectados.append(ws)
    for ws in desconectados:
        ws_clients.discard(ws)

def safe_ws_broadcast(msg):
    """Safely broadcast message to WebSocket clients."""
    if ws_loop and not ws_loop.is_closed():
        try:
            asyncio.run_coroutine_threadsafe(_ws_broadcast(msg), ws_loop)
        except Exception as e:
            print(f"Error broadcasting WebSocket message: {e}")

class Reproductor:
    def __init__(self, eventos, velocidad=1.0, on_finish=None):
        self.eventos = eventos
        self.velocidad = velocidad
        self._thread = None
        self._stop_event = threading.Event()
        self.mouse_ctl = MouseController()
        self.keyboard_ctl = KeyboardController()
        self.on_finish = on_finish
        self._listener = None
        self._ignore_until = 0

    def iniciar(self):
        """Iniciar reproducción de forma asíncrona"""
        self._stop_event.clear()
        # Ignorar cualquier entrada del usuario durante un breve periodo de
        # arranque para evitar que un movimiento involuntario cancele la
        # reproducción inmediatamente.
        self._ignore_until = time.time() + 0.2

        # Escuchar movimientos del usuario para detectar interrupciones
        self._listener = mouse.Listener(on_move=self._on_user_input,
                                        on_click=self._on_user_input)
        self._listener.start()
        self._thread = threading.Thread(target=self._reproducir, daemon=True)
        self._thread.start()

    def detener(self):
        """Detener reproducción con timeout reducido"""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.5)  # Timeout reducido
        if self._listener:
            try:
                self._listener.stop()
            finally:
                self._listener = None
        if self.on_finish:
            try:
                self.on_finish()
            except Exception as e:
                print(f"Error in finish callback: {e}")

    def _on_user_input(self, *args, **kwargs):
        """Detener reproducción si el usuario mueve el ratón manualmente"""
        if time.time() > self._ignore_until:
            self.detener()

    def _reproducir(self):
        if not self.eventos:
            return
            
        tiempo_inicio_reproduccion = time.time()
        eventos_agrupados = self._agrupar_eventos_cercanos()
        
        for i, (tipo, momento, datos) in enumerate(eventos_agrupados):
            if self._stop_event.is_set():
                break

            tiempo_objetivo = tiempo_inicio_reproduccion + (momento / self.velocidad)
            tiempo_actual = time.time()
            tiempo_espera = tiempo_objetivo - tiempo_actual

            if tiempo_espera > 0:
                if self._stop_event.wait(tiempo_espera):
                    break

            try:
                self._ignore_until = time.time() + 0.03
                self._ejecutar_evento(tipo, datos)
                self._ignore_until = time.time() + 0.03
            except Exception as e:
                print(f"Error ejecutando evento: {e}")
                continue

        # Finalizar reproducción
        if self._listener:
            try:
                self._listener.stop()
            finally:
                self._listener = None
        if self.on_finish:
            try:
                self.on_finish()
            except Exception as e:
                print(f"Error in finish callback: {e}")

    def _agrupar_eventos_cercanos(self):
        """Optimización: agrupar eventos más eficientemente"""
        if not self.eventos:
            return []
        
        eventos_agrupados = []
        ultimo_move_time = -1
        
        for tipo, momento, datos in self.eventos:
            if tipo == 'mouse_move' and momento - ultimo_move_time < 0.01:
                if eventos_agrupados and eventos_agrupados[-1][0] == 'mouse_move':
                    eventos_agrupados[-1] = (tipo, momento, datos)
                else:
                    eventos_agrupados.append((tipo, momento, datos))
                ultimo_move_time = momento
            else:
                eventos_agrupados.append((tipo, momento, datos))
                if tipo == 'mouse_move':
                    ultimo_move_time = momento
        
        return eventos_agrupados

    def _ejecutar_evento(self, tipo, datos):
        """Ejecutar evento con validaciones optimizadas"""
        if tipo == 'mouse_move':
            x, y = datos
            if 0 <= x < 10000 and 0 <= y < 10000:
                self.mouse_ctl.position = (x, y)
        
        elif tipo == 'mouse_click':
            x, y, button, pressed = datos
            if 0 <= x < 10000 and 0 <= y < 10000:
                self.mouse_ctl.position = (x, y)
                time.sleep(0.005)  # Reducir pausa
                if pressed:
                    self.mouse_ctl.press(button)
                else:
                    self.mouse_ctl.release(button)
        
        elif tipo == 'mouse_scroll':
            x, y, dx, dy = datos
            if 0 <= x < 10000 and 0 <= y < 10000:
                self.mouse_ctl.position = (x, y)
                self.mouse_ctl.scroll(dx, dy)
        
        elif tipo == 'key_press':
            self.keyboard_ctl.press(datos)
        
        elif tipo == 'key_release':
            self.keyboard_ctl.release(datos)

def actualizar_estado_cache():
    """Actualizar cache del estado del sistema"""
    global estado_cache, estado_cache_timestamp

    with estado_cache_lock:
        with lock:
            acciones = len(eventos)
            duracion = 0.0
            if grabando:
                duracion = time.time() - tiempo_inicio
            elif eventos:
                duracion = max(m for _, m, _ in eventos)

            tamano = grabacion_en_memoria.getbuffer().nbytes if not grabando else 0
            fps = acciones / duracion if duracion > 0 else 0.0

            estado_cache.update({
                "grabando": grabando,
                "reproduciendo": reproductor is not None and reproductor._thread and reproductor._thread.is_alive(),
                "tiene_grabacion": grabacion_en_memoria.tell() > 0 or len(eventos) > 0,
                "velocidad": velocidad_reproduccion,
                "duracion": duracion,
                "acciones": acciones,
                "tamano": tamano,
                "fps": fps
            })
            estado_cache_timestamp = time.time()

def _reproduccion_finalizada():
    """Callback para actualizar estado cuando termina la reproducción"""
    global reproductor
    with lock:
        reproductor = None
    actualizar_estado_cache()

def procesar_eventos_async():
    """Procesador de eventos en background"""
    while True:
        try:
            evento = eventos_queue.get(timeout=1)
            if evento is None:  # Señal para terminar
                break
            
            tipo, momento, datos = evento
            with lock:
                eventos.append((tipo, momento, datos))
            
            eventos_queue.task_done()
        except queue.Empty:
            continue
        except Exception as e:
            print(f"Error procesando evento: {e}")

# Iniciar procesador de eventos en background
evento_processor_thread = threading.Thread(target=procesar_eventos_async, daemon=True)
evento_processor_thread.start()

@app.route("/grabar", methods=["POST"])
def iniciar_grabacion():
    global eventos, grabando, ml, kl, tiempo_inicio, grabacion_en_memoria, config_grabacion
    
    opts = request.get_json(force=True) if request.data else {}
    with lock:
        if grabando:
            return jsonify({"error": "Ya hay una grabación en curso"}), 400

        eventos = []
        grabacion_en_memoria = BytesIO()
        grabando = True
        tiempo_inicio = time.time()

        config_grabacion.update({
            "mouseMove": bool(opts.get("mouseMove", True)),
            "mouseClick": bool(opts.get("mouseClick", True)),
            "keyboard": bool(opts.get("keyboard", True)),
            "smartCapture": bool(opts.get("smartCapture", False)),
            "fps": max(1, min(144, int(opts.get("fps", 60))))
        })
    
    # Actualizar cache inmediatamente
    actualizar_estado_cache()
    
    ultima_posicion = [None]
    ultimo_tiempo_move = [0]
    intervalo_move = 1.0 / config_grabacion.get("fps", 60)
    ultima_tecla = [None]

    def on_click(x, y, button, pressed):
        if grabando and config_grabacion.get("mouseClick", True):
            try:
                eventos_queue.put_nowait(('mouse_click', time.time() - tiempo_inicio, (x, y, button, pressed)))
                btn_name = str(button).split('.')[-1]
                safe_ws_broadcast({'tipo': 'mouse_click', 'data': {'x': x, 'y': y, 'button': btn_name, 'pressed': pressed}})
            except queue.Full:
                pass  # Descartar evento si la cola está llena

    def on_move(x, y):
        if grabando and config_grabacion.get("mouseMove", True):
            tiempo_actual = time.time()
            if tiempo_actual - ultimo_tiempo_move[0] >= intervalo_move:
                actual = (x, y)
                if not config_grabacion.get("smartCapture") or actual != ultima_posicion[0]:
                    try:
                        eventos_queue.put_nowait(('mouse_move', tiempo_actual - tiempo_inicio, actual))
                        ultima_posicion[0] = actual
                        ultimo_tiempo_move[0] = tiempo_actual
                        safe_ws_broadcast({'tipo': 'mouse_move', 'data': {'x': x, 'y': y}})
                    except queue.Full:
                        pass

    def on_scroll(x, y, dx, dy):
        if grabando and config_grabacion.get("mouseMove", True):
            try:
                eventos_queue.put_nowait(('mouse_scroll', time.time() - tiempo_inicio, (x, y, dx, dy)))
                safe_ws_broadcast({'tipo': 'mouse_scroll', 'data': {'x': x, 'y': y, 'dx': dx, 'dy': dy}})
            except queue.Full:
                pass

    def on_press(key):
        if grabando and config_grabacion.get("keyboard", True):
            try:
                key_str = getattr(key, 'char', None) or str(key)
                if not config_grabacion.get("smartCapture") or ultima_tecla[0] != ("press", key_str):
                    eventos_queue.put_nowait(('key_press', time.time() - tiempo_inicio, key))
                    safe_ws_broadcast({'tipo': 'key_press', 'data': {'key': key_str}})
                ultima_tecla[0] = ("press", key_str)
            except queue.Full:
                pass

    def on_release(key):
        if grabando and config_grabacion.get("keyboard", True):
            try:
                key_str = getattr(key, 'char', None) or str(key)
                if not config_grabacion.get("smartCapture") or ultima_tecla[0] != ("release", key_str):
                    eventos_queue.put_nowait(('key_release', time.time() - tiempo_inicio, key))
                    safe_ws_broadcast({'tipo': 'key_release', 'data': {'key': key_str}})
                ultima_tecla[0] = ("release", key_str)
            except queue.Full:
                pass

    ml = mouse.Listener(on_click=on_click, on_move=on_move, on_scroll=on_scroll)
    kl = pynput_keyboard.Listener(on_press=on_press, on_release=on_release)
    
    ml.start()
    kl.start()
    
    return jsonify({"status": "grabando"})

@app.route("/detener", methods=["POST"])
def detener_grabacion():
    global grabando, ml, kl, grabacion_en_memoria
    
    with lock:
        if not grabando:
            return jsonify({"error": "No hay grabación activa"}), 400
        
        grabando = False
        
        if ml:
            ml.stop()
            ml = None
        if kl:
            kl.stop()
            kl = None
        
        # Procesar eventos restantes en la cola
        eventos_restantes = []
        while not eventos_queue.empty():
            try:
                evento = eventos_queue.get_nowait()
                eventos_restantes.append(evento)
            except queue.Empty:
                break
        
        eventos.extend(eventos_restantes)
        
        # Guardar eventos en memoria de forma asíncrona
        def guardar_async():
            global grabacion_en_memoria
            grabacion_temp = BytesIO()
            pickle.dump(eventos, grabacion_temp)
            grabacion_temp.seek(0)
            grabacion_en_memoria = grabacion_temp
        
        thread_pool.submit(guardar_async)
        num_eventos = len(eventos)
    
    # Actualizar cache
    actualizar_estado_cache()
    
    return jsonify({"status": "grabación detenida", "eventos": num_eventos})

@app.route("/reproducir", methods=["POST"])
def reproducir():
    global reproductor
    
    with lock:
        if reproductor and reproductor._thread and reproductor._thread.is_alive():
            return jsonify({"error": "Ya hay una reproducción en curso"}), 400
        
        try:
            grabacion_en_memoria.seek(0)
            eventos_reproducir = pickle.load(grabacion_en_memoria)
        except:
            return jsonify({"error": "No hay grabación en memoria"}), 400
        
        if not eventos_reproducir:
            return jsonify({"error": "La grabación está vacía"}), 400
        
        # Obtener velocidad del request
        velocidad = request.json.get("velocidad", velocidad_reproduccion) if request.json else velocidad_reproduccion
        
        reproductor = Reproductor(eventos_reproducir, velocidad, on_finish=_reproduccion_finalizada)
    
    # Iniciar reproducción de forma asíncrona
    thread_pool.submit(reproductor.iniciar)
    
    # Actualizar cache
    actualizar_estado_cache()
    
    return jsonify({"status": "reproducción iniciada", "eventos": len(eventos_reproducir)})

@app.route("/detener_reproduccion", methods=["POST"])
def detener_reproduccion():
    global reproductor
    
    with lock:
        if reproductor:
            # Detener de forma asíncrona
            thread_pool.submit(reproductor.detener)
            reproductor = None
            actualizar_estado_cache()
            return jsonify({"status": "reproducción detenida"})
    
    return jsonify({"error": "no hay reproducción activa"}), 400

@app.route("/velocidad", methods=["POST"])
def cambiar_velocidad():
    global velocidad_reproduccion
    
    try:
        data = request.get_json(force=True) if request.data else {}
        nueva_velocidad = data.get("velocidad")
    except:
        return jsonify({"error": "JSON inválido"}), 400
    
    if not nueva_velocidad or nueva_velocidad <= 0:
        return jsonify({"error": "Velocidad inválida"}), 400
    
    with lock:
        velocidad_reproduccion = nueva_velocidad
    
    actualizar_estado_cache()
    
    return jsonify({"status": "velocidad actualizada", "velocidad": nueva_velocidad})

@app.route("/guardar", methods=["POST"])
def guardar_archivo():
    try:
        data = request.get_json(force=True) if request.data else {}
        path = data.get("ruta", "macro.pkl")
    except:
        return jsonify({"error": "JSON inválido"}), 400
    
    def guardar_async():
        try:
            with lock:
                grabacion_en_memoria.seek(0)
                data = grabacion_en_memoria.read()
                grabacion_en_memoria.seek(0)
            
            with open(path, "wb") as f:
                f.write(data)
            
            return {"status": "guardado", "ruta": path}
        except Exception as e:
            return {"error": f"Error al guardar: {str(e)}"}
    
    # Ejecutar guardado de forma asíncrona
    future = thread_pool.submit(guardar_async)
    try:
        result = future.result(timeout=5)  # Timeout de 5 segundos
        if "error" in result:
            return jsonify(result), 500
        return jsonify(result)
    except:
        return jsonify({"error": "Timeout al guardar archivo"}), 500

@app.route("/cargar", methods=["POST"])
def cargar_archivo():
    global grabacion_en_memoria
    
    try:
        data = request.get_json(force=True) if request.data else {}
        path = data.get("ruta")
    except:
        return jsonify({"error": "JSON inválido"}), 400
    
    if not path or not os.path.exists(path):
        return jsonify({"error": "archivo no encontrado"}), 404
    
    def cargar_async():
        try:
            with open(path, "rb") as f:
                file_data = f.read()
            
            # Verificar que sea un archivo pickle válido
            eventos_cargados = pickle.loads(file_data)
            if not isinstance(eventos_cargados, list):
                raise ValueError("El archivo no contiene una lista de eventos")
            
            return file_data, len(eventos_cargados)
        except Exception as e:
            raise e
    
    # Ejecutar carga de forma asíncrona
    future = thread_pool.submit(cargar_async)
    try:
        file_data, num_eventos = future.result(timeout=5)
        
        with lock:
            grabacion_en_memoria = BytesIO(file_data)
        
        actualizar_estado_cache()
        
        return jsonify({"status": "cargado", "ruta": path, "eventos": num_eventos})
    
    except Exception as e:
        return jsonify({"error": f"Error al cargar archivo: {str(e)}"}), 500

@app.route("/estado", methods=["GET"])
def obtener_estado():
    """Endpoint optimizado para verificar el estado actual del sistema"""
    # Verificar si el cache es reciente (menos de 100ms)
    with estado_cache_lock:
        if time.time() - estado_cache_timestamp < 0.1:
            return jsonify(estado_cache)
    
    # Actualizar cache si es necesario
    actualizar_estado_cache()
    
    with estado_cache_lock:
        return jsonify(estado_cache.copy())

# Endpoint para health check rápido
@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"status": "ok"}), 200

# Optimización: Configurar Flask para mejor rendimiento
app.config['JSON_SORT_KEYS'] = False
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False

if __name__ == "__main__":
    # Enable DPI awareness for consistent coordinates on Windows
    if platform.system() == "Windows":
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except:
            pass
    
    print("Iniciando servidor Flask optimizado en puerto 5002...")
    
    # Configuración optimizada para producción
    app.run(
        port=5002, 
        debug=False, 
        threaded=True,
        host='127.0.0.1',  # Especificar host explícitamente
        use_reloader=False,  # Desactivar auto-reload para mejor rendimiento
        use_debugger=False   # Desactivar debugger
    )
