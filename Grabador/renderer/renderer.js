const API_URL = 'http://127.0.0.1:5002';

async function enviarComando(endpoint, data = {}) {
    try {
        const response = await fetch(`${API_URL}/${endpoint}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(data)
        });
        const resultado = await response.json();
        console.log(`✔️ Comando ${endpoint} ejecutado:`, resultado);
    } catch (error) {
        console.error(`❌ Error al ejecutar ${endpoint}:`, error);
        mostrarNotificacion('Backend no disponible', 'No se pudo comunicar con el servidor.');
    }
}

let estado = "idle"; // idle | recording | paused | playing
let hayGrabacion = false;
let bloqueado = false;
let ws;

function mostrarNotificacion(titulo, cuerpo) {
    if (!window.Notification) return;
    if (Notification.permission === "granted") {
        new Notification(titulo, { body: cuerpo });
    } else if (Notification.permission !== "denied") {
        Notification.requestPermission().then(p => {
            if (p === "granted") {
                new Notification(titulo, { body: cuerpo });
            }
        });
    }
}

async function verificarBackend() {
    try {
        const res = await fetch(`${API_URL}/ping`);
        if (!res.ok) throw new Error('Ping failed');
        return true;
    } catch (_) {
        mostrarNotificacion('Backend no disponible', 'Asegúrate de que el servidor esté en ejecución.');
        return false;
    }
}

function bloquearTemporalmente(ms = 500) {
    bloqueado = true;
    setTimeout(() => bloqueado = false, ms);
}

function connectWS() {
    if (ws) return;
    ws = new WebSocket('ws://127.0.0.1:8765');
    ws.onmessage = (ev) => {
        if (estado !== 'recording') return;
        try {
            const { tipo, data } = JSON.parse(ev.data);
            window.addActivityEntry?.(tipo, JSON.stringify(data), tipo);
        } catch (e) {
            console.error('WS parse error', e);
        }
    };
    ws.onclose = () => { ws = null; };
}

function disconnectWS() {
    if (ws) {
        ws.close();
        ws = null;
    }
}

async function actualizarEstado() {
    try {
        const res = await fetch(`${API_URL}/estado`);
        const data = await res.json();
        hayGrabacion = data.tiene_grabacion;

        if (data.interrumpido) {
            mostrarNotificacion("Reproducción interrumpida", "Se detuvo la reproducción por intervención del usuario.");
        }

        // Actualizar métricas en la UI si los elementos existen
        const durElem = document.getElementById("recording-time");
        const accElem = document.getElementById("action-count");
        const sizeElem = document.getElementById("file-size");
        const fpsElem = document.getElementById("fps-counter");
        if (durElem) {
            const d = Math.floor(data.duracion);
            const h = Math.floor(d / 3600).toString().padStart(2, "0");
            const m = Math.floor((d % 3600) / 60).toString().padStart(2, "0");
            const s = (d % 60).toString().padStart(2, "0");
            durElem.textContent = `${h}:${m}:${s}`;
        }
        if (accElem) accElem.textContent = data.acciones;
        if (sizeElem) sizeElem.textContent = `${(data.tamano / 1024).toFixed(1)} KB`;
        if (fpsElem) fpsElem.textContent = data.fps.toFixed(1);

        if (estado !== "paused") {
            if (data.grabando) {
                estado = "recording";
            } else if (data.reproduciendo) {
                estado = "playing";
            } else {
                estado = "idle";
            }
        }

        // Sincronizar estado de reproducción
        actualizarUI();
    } catch (error) {
        console.error("Error obteniendo estado", error);
    }
}

function iniciarEstadoPolling() {
    actualizarEstado();
    setInterval(actualizarEstado, 1000);
}

window.startBackendRecording = () => {
    enviarComando("grabar");
    connectWS();
    if (window.startCapture) {
        try { window.startCapture(); } catch (e) { console.error(e); }
    }
    actualizarEstado();
};

window.stopBackendRecording = () => {
    enviarComando("detener");
    disconnectWS();
    if (window.stopCapture) {
        try { window.stopCapture(); } catch (e) { console.error(e); }
    }
    actualizarEstado();
};

window.playbackRecording = () => {
    enviarComando("reproducir");
    actualizarEstado();
};

function el(id) {
    return document.getElementById(id);
}

function actualizarUI() {
    // Todos los botones siempre visibles
    const btnGrabar = el("btnGrabar");
    const btnPausar = el("btnPausar");
    const btnDetener = el("btnDetener");
    const btnReproducir = el("btnReproducir");
    const recordText = el("record-text");

    if (btnGrabar) {
        btnGrabar.style.display = "inline-flex";
        btnGrabar.disabled = (estado === "recording" || estado === "paused");
    }
    if (btnPausar) btnPausar.style.display = "inline-flex";
    if (btnDetener) btnDetener.style.display = "inline-flex";
    if (btnReproducir) btnReproducir.style.display = "inline-flex";

    btnGrabar.style.display = "inline-flex";
    btnPausar.style.display = "inline-flex";
    btnDetener.style.display = "inline-flex";
    btnReproducir.style.display = "inline-flex";

    if (recordText) {
        recordText.innerText = (estado === "recording" || estado === "paused") ? "Grabando..." : "Grabar";
    }

    if (btnPausar) {
        btnPausar.querySelector("span").innerText = (estado === "paused") ? "Reanudar" : "Pausar";
    }
}

window.toggleRecording = () => {
    if (bloqueado) return;
    bloquearTemporalmente();

    if (estado === "idle") {
        estado = "recording";
        startBackendRecording();
    } else if (estado === "recording" || estado === "paused") {
        estado = "idle";
        stopBackendRecording();
        hayGrabacion = true;
    }
    actualizarUI();
};

window.pauseRecording = () => {
    if (bloqueado) return;
    bloquearTemporalmente();

    if (estado === "recording") {
        estado = "paused";
        if (window.stopCapture) {
            try { window.stopCapture(); } catch (e) { console.error(e); }
        }
    } else if (estado === "paused") {
        estado = "recording";
        if (window.startCapture) {
            try { window.startCapture(); } catch (e) { console.error(e); }
        }
    }
    actualizarUI();
};

window.stopRecording = () => {
    if (bloqueado) return;
    bloquearTemporalmente();

    if (estado !== "idle") {
        estado = "idle";
        stopBackendRecording();
        if (window.stopCapture) {
            try { window.stopCapture(); } catch (e) { console.error(e); }
        }
        hayGrabacion = true;
        actualizarUI();
    }
};

window.startPlayback = () => {
    if (bloqueado || estado !== "idle" || !hayGrabacion) return;
    bloquearTemporalmente();

    estado = "playing";
    playbackRecording();
    actualizarUI();
    actualizarEstado();
};

function conectarControles() {
    el("btnGrabar")?.addEventListener("click", window.toggleRecording);
    el("btnDetener")?.addEventListener("click", window.stopRecording);
    el("btnPausar")?.addEventListener("click", window.pauseRecording);
    el("btnReproducir")?.addEventListener("click", window.startPlayback);
}

async function init() {
    conectarControles();
    actualizarUI();
    if (await verificarBackend()) {
        iniciarEstadoPolling();
    }
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
} else {
    init();
}
