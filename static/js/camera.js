// Optional camera buzz for the in-person manual reader.
//
// The camera stays completely untouched until the moderator clicks Enable:
// no getUserMedia call, and no permission prompt, runs on page load. Frames are
// only sent while a manual match is connected and the moderator has opted in.
(function () {
    'use strict';

    const SEATS = ['A-1', 'A-C', 'A-3', 'A-4', 'A-5', 'B-1', 'B-C', 'B-3', 'B-4', 'B-5'];
    const FRAME_INTERVAL_MS = 200;

    let currentStream = null;
    let desiredOn = false;
    let roomEnabled = false;
    let roomMode = 'in_person';
    let frameTimer = null;
    let sendingFrame = false;
    let mappingData = {};
    let currentSeatIdx = 0;
    let isDrawing = false;
    let startX = 0;
    let startY = 0;
    let drawPointerId = null;

    function $(id) {
        return document.getElementById(id);
    }

    function setStatus(text, tone) {
        const el = $('camera-setup-status');
        if (!el) return;
        el.textContent = text == null ? '' : String(text);
        el.className = 'text-[11px] ' + (tone === 'good' ? 'text-neonGreen' : tone === 'bad' ? 'text-neonPink' : 'text-slate-500');
    }

    function isConnected() {
        return Boolean(window.ws && window.ws.readyState === WebSocket.OPEN);
    }

    function sendCameraEvent(type, payload) {
        if (!isConnected() || typeof window.sendEvent !== 'function') return false;
        window.sendEvent(type, payload || {});
        return true;
    }

    function updateUi() {
        const on = Boolean(desiredOn);
        const setupButton = $('btn-camera-enable');
        if (setupButton) {
            setupButton.textContent = on ? 'Disable camera buzz' : 'Enable camera buzz';
            setupButton.setAttribute('aria-pressed', String(on));
            setupButton.classList.toggle('border-neonPink/60', on);
            setupButton.classList.toggle('text-neonPink', on);
            setupButton.classList.toggle('border-neonGreen/60', !on);
            setupButton.classList.toggle('text-neonGreen', !on);
        }
        const panel = $('camera-panel-body');
        if (panel) panel.classList.toggle('hidden', !on);
        const hudButton = $('btn-hud-camera');
        if (hudButton) {
            const inPerson = roomMode === 'in_person' || roomMode === 'in_person_ai';
            hudButton.classList.toggle('hidden', !inPerson);
            hudButton.classList.toggle('is-on', on);
            hudButton.textContent = on ? 'CAM ON' : 'CAM OFF';
            hudButton.setAttribute('aria-pressed', String(on));
            hudButton.title = on ? 'Camera buzz is on' : 'Camera buzz is off';
        }
    }

    function syncCanvasToStage() {
        const canvas = $('calib-canvas');
        const stage = canvas ? canvas.parentElement : null;
        if (!canvas || !stage) return;
        const width = Math.max(1, Math.round(stage.clientWidth));
        const height = Math.max(1, Math.round(stage.clientHeight));
        if (canvas.width !== width || canvas.height !== height) {
            canvas.width = width;
            canvas.height = height;
        }
        redraw();
    }

    function redraw() {
        const canvas = $('calib-canvas');
        if (!canvas) return;
        const ctx = canvas.getContext('2d');
        if (!ctx) return;
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        Object.keys(mappingData).forEach((seat) => {
            const box = mappingData[seat];
            if (!Array.isArray(box) || box.length !== 4) return;
            const x = box[0] * canvas.width;
            const y = box[1] * canvas.height;
            const w = (box[2] - box[0]) * canvas.width;
            const h = (box[3] - box[1]) * canvas.height;
            ctx.strokeStyle = '#34d399';
            ctx.lineWidth = 2;
            ctx.strokeRect(x, y, w, h);
            ctx.font = 'bold 12px monospace';
            ctx.fillStyle = '#34d399';
            ctx.fillText(seat, x + 2, Math.max(12, y - 4));
        });
    }

    function updateCalibrationInstruction() {
        const instruction = $('calib-instruction');
        if (!instruction) return;
        if (currentSeatIdx >= SEATS.length) {
            instruction.textContent = 'All seats mapped. Click Save zones.';
            instruction.className = 'camera-calib-instruction is-complete';
            return;
        }
        instruction.textContent = 'Draw Box: ' + SEATS[currentSeatIdx];
        instruction.className = 'camera-calib-instruction';
    }

    async function loadStream(deviceId) {
        const constraints = {
            video: {
                width: { ideal: 640 },
                height: { ideal: 360 }
            }
        };
        if (deviceId) constraints.video.deviceId = { exact: deviceId };
        return navigator.mediaDevices.getUserMedia(constraints);
    }

    async function enable() {
        if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== 'function') {
            setStatus('This browser does not support camera access.', 'bad');
            return false;
        }
        if (currentStream) {
            desiredOn = true;
            updateUi();
            startFramePump();
            return true;
        }

        setStatus('Requesting camera permission...');
        try {
            currentStream = await loadStream($('camera-select') ? $('camera-select').value : null);
        } catch (error) {
            console.warn('Camera access was not granted', error);
            desiredOn = false;
            updateUi();
            setStatus('Camera permission was not granted. Manual buzzers still work.', 'bad');
            return false;
        }

        desiredOn = true;
        updateUi();
        const video = $('video-stream');
        if (video) {
            video.srcObject = currentStream;
            video.play().catch(() => {});
        }
        const stage = video ? video.parentElement : null;
        if (stage) {
            stage.classList.add('is-live');
            if (typeof ResizeObserver === 'function') {
                if (stage._cameraResizeObserver) stage._cameraResizeObserver.disconnect();
                stage._cameraResizeObserver = new ResizeObserver(syncCanvasToStage);
                stage._cameraResizeObserver.observe(stage);
            }
        }
        window.addEventListener('resize', syncCanvasToStage);
        setTimeout(syncCanvasToStage, 0);
        await refreshDevices();
        switchToDevice($('camera-select') ? $('camera-select').value : null);
        updateCalibrationInstruction();
        setStatus('Camera ready. It arms only while a toss-up is live.', 'good');
        if (isConnected()) startForMatch();
        return true;
    }

    function switchToDevice(deviceId) {
        if (!deviceId) return;
        const track = currentStream && currentStream.getVideoTracks ? currentStream.getVideoTracks()[0] : null;
        if (!track) return;
        const settings = track.getSettings ? track.getSettings() : {};
        if (settings.deviceId === deviceId) return;
        stopTracks();
        loadStream(deviceId).then((stream) => {
            currentStream = stream;
            const video = $('video-stream');
            if (video) {
                video.srcObject = stream;
                video.play().catch(() => {});
            }
            startFramePump();
        }).catch((error) => {
            console.warn('Unable to switch camera', error);
            setStatus('Could not open that camera.', 'bad');
        });
    }

    function stopTracks() {
        if (!currentStream) return;
        currentStream.getTracks().forEach((track) => track.stop());
        currentStream = null;
    }

    function stopFramePump() {
        if (frameTimer) clearInterval(frameTimer);
        frameTimer = null;
        sendingFrame = false;
    }

    function startFramePump() {
        if (!desiredOn || !currentStream || frameTimer) return;
        frameTimer = setInterval(sendFrame, FRAME_INTERVAL_MS);
    }

    function sendFrame() {
        if (!desiredOn || sendingFrame) return;
        if (!isConnected()) return;
        const video = $('video-stream');
        if (!video || !video.videoWidth || !video.videoHeight) return;

        sendingFrame = true;
        const canvas = document.createElement('canvas');
        canvas.width = 640;
        canvas.height = 360;
        const ctx = canvas.getContext('2d');
        if (!ctx) {
            sendingFrame = false;
            return;
        }
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
        canvas.toBlob((blob) => {
            if (!blob) {
                sendingFrame = false;
                return;
            }
            const reader = new FileReader();
            reader.onloadend = () => {
                sendingFrame = false;
                if (!desiredOn || !isConnected()) return;
                if (typeof window.sendEvent === 'function') {
                    window.sendEvent('PROCESS_FRAME', { frame_data: reader.result });
                }
            };
            reader.onerror = () => { sendingFrame = false; };
            reader.readAsDataURL(blob);
        }, 'image/jpeg', 0.55);
    }

    async function refreshDevices() {
        const select = $('camera-select');
        if (!select || !navigator.mediaDevices || typeof navigator.mediaDevices.enumerateDevices !== 'function') return;
        try {
            const devices = await navigator.mediaDevices.enumerateDevices();
            const cameras = devices.filter((device) => device.kind === 'videoinput');
            const current = select.value;
            select.replaceChildren();
            if (!cameras.length) {
                const option = document.createElement('option');
                option.value = '';
                option.textContent = 'Default camera';
                select.appendChild(option);
                return;
            }
            cameras.forEach((device, index) => {
                const option = document.createElement('option');
                option.value = device.deviceId;
                option.textContent = device.label || ('Camera ' + (index + 1));
                select.appendChild(option);
            });
            if (current && cameras.some((device) => device.deviceId === current)) select.value = current;
        } catch (error) {
            console.warn('Unable to list cameras', error);
        }
    }

    function applyState(state) {
        const data = state && typeof state === 'object' ? state : {};
        if (typeof data.enabled === 'boolean') roomEnabled = data.enabled;
        if (typeof data.mode === 'string') roomMode = data.mode;
        if (roomEnabled && desiredOn) startFramePump();
        updateUi();
    }

    function startForMatch() {
        if (!desiredOn) return;
        if (!currentStream) {
            enable().then((opened) => { if (opened) startForMatch(); });
            return;
        }
        sendCameraEvent('SET_CAMERA_BUZZ', { enabled: true });
        if (Object.keys(mappingData).length) {
            sendCameraEvent('SAVE_CALIBRATION', mappingData);
        }
        startFramePump();
        setStatus('Camera buzz connected. Detection arms during each live read.', 'good');
    }

    function disable() {
        desiredOn = false;
        roomEnabled = false;
        stopFramePump();
        stopTracks();
        const video = $('video-stream');
        if (video) {
            video.srcObject = null;
            if (video.parentElement) video.parentElement.classList.remove('is-live');
        }
        redraw();
        sendCameraEvent('SET_CAMERA_BUZZ', { enabled: false });
        updateUi();
        setStatus('Camera off. Manual buzzers still work.');
    }

    function toggle() {
        if (desiredOn) {
            disable();
            return;
        }
        enable().then((opened) => {
            if (opened && isConnected()) startForMatch();
        });
    }

    function saveCalibration() {
        if (!Object.keys(mappingData).length) {
            setStatus('Draw at least one zone before saving.', 'bad');
            return;
        }
        if (isConnected() && desiredOn) {
            sendCameraEvent('SAVE_CALIBRATION', mappingData);
            sendCameraEvent('CALIBRATE_BASELINE', {});
            setStatus('Zones saved and lighting re-zeroed.', 'good');
        } else {
            setStatus('Zones staged. They are sent when the camera match starts.', 'good');
        }
    }

    function zero() {
        if (isConnected() && desiredOn) {
            sendCameraEvent('CALIBRATE_BASELINE', {});
            setStatus('Re-zeroing the current room lighting...', 'good');
        } else {
            setStatus('Start the match with camera buzz on to re-zero.', 'bad');
        }
    }

    function resetCalibration() {
        mappingData = {};
        currentSeatIdx = 0;
        redraw();
        updateCalibrationInstruction();
        if (isConnected() && desiredOn) sendCameraEvent('RESET_CALIBRATION', {});
        setStatus('Zones cleared. Draw them again before the next match.');
    }

    function getPoint(event) {
        const canvas = $('calib-canvas');
        if (!canvas) return { x: 0, y: 0 };
        const rect = canvas.getBoundingClientRect();
        const x = ((event.clientX - rect.left) / Math.max(1, rect.width)) * canvas.width;
        const y = ((event.clientY - rect.top) / Math.max(1, rect.height)) * canvas.height;
        return { x: Math.max(0, Math.min(canvas.width, x)), y: Math.max(0, Math.min(canvas.height, y)) };
    }

    function beginDraw(event) {
        if (!desiredOn || currentSeatIdx >= SEATS.length) return;
        const canvas = $('calib-canvas');
        if (!canvas) return;
        syncCanvasToStage();
        isDrawing = true;
        drawPointerId = event.pointerId;
        const point = getPoint(event);
        startX = point.x;
        startY = point.y;
        if (canvas.setPointerCapture) canvas.setPointerCapture(event.pointerId);
        event.preventDefault();
    }

    function moveDraw(event) {
        if (!isDrawing) return;
        const canvas = $('calib-canvas');
        if (!canvas) return;
        const ctx = canvas.getContext('2d');
        if (!ctx) return;
        const point = getPoint(event);
        redraw();
        ctx.strokeStyle = '#fbbf24';
        ctx.lineWidth = 2;
        ctx.strokeRect(startX, startY, point.x - startX, point.y - startY);
        event.preventDefault();
    }

    function endDraw(event) {
        if (!isDrawing) return;
        isDrawing = false;
        const canvas = $('calib-canvas');
        if (!canvas) return;
        const point = getPoint(event);
        const x1 = Math.min(startX, point.x) / canvas.width;
        const y1 = Math.min(startY, point.y) / canvas.height;
        const x2 = Math.max(startX, point.x) / canvas.width;
        const y2 = Math.max(startY, point.y) / canvas.height;

        if (x2 - x1 > 0.005 && y2 - y1 > 0.005 && currentSeatIdx < SEATS.length) {
            mappingData[SEATS[currentSeatIdx]] = [
                Number(x1.toFixed(3)), Number(y1.toFixed(3)),
                Number(x2.toFixed(3)), Number(y2.toFixed(3))
            ];
            currentSeatIdx += 1;
            updateCalibrationInstruction();
        }
        redraw();
        if (drawPointerId != null && canvas.releasePointerCapture) {
            try { canvas.releasePointerCapture(drawPointerId); } catch (error) { /* already released */ }
        }
        drawPointerId = null;
        event.preventDefault();
    }

    function bindCanvas() {
        const canvas = $('calib-canvas');
        if (!canvas || canvas.dataset.cameraBound === '1') return;
        canvas.dataset.cameraBound = '1';
        canvas.addEventListener('pointerdown', beginDraw);
        canvas.addEventListener('pointermove', moveDraw);
        canvas.addEventListener('pointerup', endDraw);
        canvas.addEventListener('pointercancel', endDraw);
        document.addEventListener('keydown', (event) => {
            if (event.key !== 'Escape' || !isDrawing) return;
            isDrawing = false;
            drawPointerId = null;
            redraw();
        });
        const video = $('video-stream');
        if (video) video.addEventListener('loadedmetadata', syncCanvasToStage);
        window.addEventListener('resize', syncCanvasToStage);
    }

    bindCanvas();
    updateUi();
    updateCalibrationInstruction();

    window.CameraBuzz = {
        toggle: toggle,
        startForMatch: startForMatch,
        stop: disable,
        applyState: applyState,
        refreshDevices: refreshDevices,
        saveCalibration: saveCalibration,
        resetCalibration: resetCalibration,
        zero: zero,
        isEnabled: () => desiredOn
    };
})();
