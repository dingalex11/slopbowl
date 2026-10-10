let currentStream = null;
let isStreamingFrames = false;

async function loadCameras() {
    try {
        await navigator.mediaDevices.getUserMedia({ video: true }); 
        const devices = await navigator.mediaDevices.enumerateDevices();
        const videoDevices = devices.filter(device => device.kind === 'videoinput');
        const select = document.getElementById('camera-select');
        
        if (!select) return;

        select.innerHTML = ''; 
        videoDevices.forEach((device, index) => {
            const option = document.createElement('option');
            option.value = device.deviceId;
            option.text = device.label || `Camera ${index + 1}`;
            select.appendChild(option);
        });
        
        if (videoDevices.length > 0) {
            startCamera(videoDevices[0].deviceId);
        }
    } catch(err) {
        console.error("Error loading cameras:", err);
    }
}
window.addEventListener('load', loadCameras);

async function startCamera(deviceId = null) {
    if (currentStream) {
        currentStream.getTracks().forEach(track => track.stop());
    }

    const constraints = {
        video: {
            width: { ideal: 640 },
            height: { ideal: 360 },
            deviceId: deviceId ? { exact: deviceId } : undefined
        }
    };

    try {
        currentStream = await navigator.mediaDevices.getUserMedia(constraints);
        
        const videoSetup = document.getElementById('video-stream');
        const videoMatch = document.getElementById('match-video-stream');
        if(videoSetup) videoSetup.srcObject = currentStream;
        if(videoMatch) videoMatch.srcObject = currentStream;

        const overlayText = document.getElementById('calib-overlay-text');
        if (overlayText) overlayText.classList.add('hidden');
        
        // Start non-blocking frame loop
        if (!isStreamingFrames) {
            isStreamingFrames = true;
            requestAnimationFrame(sendFrameToBackend);
        }
        
    } catch (err) {
        console.error("Camera access denied:", err);
        alert("Please allow camera permissions in your browser!");
    }
}

function sendFrameToBackend() {
    if (!isStreamingFrames) return;
    
    // Throttle slightly to ~30fps if needed, or let requestAnimationFrame run free
    setTimeout(() => {
        requestAnimationFrame(sendFrameToBackend);
    }, 200);

    if (typeof ws === 'undefined' || ws.readyState !== WebSocket.OPEN) return;
    
    let video = document.getElementById('video-stream');
    if (!video || video.clientHeight === 0) {
        video = document.getElementById('match-video-stream');
    }
    
    if (!video || video.videoWidth === 0 || video.videoHeight === 0) return;

    const offscreenCanvas = document.createElement('canvas');
    offscreenCanvas.width = 640; 
    offscreenCanvas.height = 360;
    offscreenCanvas.getContext('2d').drawImage(video, 0, 0, 640, 360);

    // FIX: Use async toBlob instead of blocking toDataURL
    offscreenCanvas.toBlob((blob) => {
        if (!blob) return;
        const reader = new FileReader();
        reader.onloadend = () => {
            sendEvent('PROCESS_FRAME', { frame_data: reader.result });
        };
        reader.readAsDataURL(blob);
    }, 'image/jpeg', 0.5);
}

function changeCameraDevice(deviceId) {
    startCamera(deviceId);
}

// --- CALIBRATION LOGIC ---
const canvas = document.getElementById('calib-canvas');
const ctx = canvas ? canvas.getContext('2d') : null;
const instructionText = document.getElementById('calib-instruction');

const seatsToMap = ["A-1", "A-C", "A-3", "A-4", "A-5", "B-1", "B-C", "B-3", "B-4", "B-5"];
let currentSeatIdx = 0;
let mappingData = {};
let isDrawing = false;
let startX, startY;

function startCalibration() {
    const camSelect = document.getElementById('camera-select');
    startCamera(camSelect.value); 
    
    setTimeout(() => {
        if(canvas) {
            canvas.width = canvas.offsetWidth;
            canvas.height = canvas.offsetHeight;
        }
    }, 500); 
}

// FIX: Explicit user feedback on empty calibration save
function saveCalibration() {
    if(Object.keys(mappingData).length > 0) {
        sendEvent('SAVE_CALIBRATION', mappingData); 
        if(instructionText) {
            instructionText.innerText = "Draw Box: Saved to Backend!";
            instructionText.className = "text-[10px] font-mono font-bold text-teamA uppercase block";
        }
    } else {
        alert("⚠️ You must draw at least one bounding box on the camera feed before saving!");
    }
}

// FULL RESET LOGIC
function resetCalibration() {
    currentSeatIdx = 0;
    mappingData = {};
    if(instructionText) {
        instructionText.innerText = `Draw Box: ${seatsToMap[currentSeatIdx]}`;
        instructionText.className = "text-[10px] font-mono font-bold text-teamB uppercase block";
    }
    
    // Wipe setup canvas
    if(ctx) {
        ctx.clearRect(0, 0, canvas.width, canvas.height);
    }
    
    // Wipe match canvas
    const matchCanvas = document.getElementById('match-calib-canvas');
    if(matchCanvas) {
        const mCtx = matchCanvas.getContext('2d');
        if(mCtx) mCtx.clearRect(0, 0, matchCanvas.width, matchCanvas.height);
    }
    
    // Tell backend to delete config
    sendEvent('RESET_CALIBRATION', {});
}

function getMousePos(e) {
    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;
    return {
        x: (e.clientX - rect.left) * scaleX,
        y: (e.clientY - rect.top) * scaleY
    };
}

if(canvas) {
    canvas.addEventListener('mousedown', (e) => {
        if (currentSeatIdx >= seatsToMap.length) return;
        isDrawing = true;
        
        if (canvas.width !== canvas.offsetWidth) {
            canvas.width = canvas.offsetWidth;
            canvas.height = canvas.offsetHeight;
            drawExistingBoxes(); 
        }
        
        const pos = getMousePos(e);
        startX = pos.x;
        startY = pos.y;
    });

    canvas.addEventListener('mousemove', (e) => {
        if (!isDrawing) return;
        const pos = getMousePos(e);
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        drawExistingBoxes(); 
        ctx.strokeStyle = "#34d399";
        ctx.lineWidth = 2;
        ctx.strokeRect(startX, startY, pos.x - startX, pos.y - startY);
    });

    canvas.addEventListener('mouseup', (e) => {
        if (!isDrawing) return;
        isDrawing = false;
        const pos = getMousePos(e);

        const x1 = Math.min(startX, pos.x) / canvas.width;
        const y1 = Math.min(startY, pos.y) / canvas.height;
        const x2 = Math.max(startX, pos.x) / canvas.width;
        const y2 = Math.max(startY, pos.y) / canvas.height;

        if (x2 - x1 < 0.0001 || y2 - y1 < 0.0001) {
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            drawExistingBoxes();
            return;
        }

        mappingData[seatsToMap[currentSeatIdx]] = [
            parseFloat(x1.toFixed(3)), parseFloat(y1.toFixed(3)), 
            parseFloat(x2.toFixed(3)), parseFloat(y2.toFixed(3))
        ];

        currentSeatIdx++;
        if (currentSeatIdx < seatsToMap.length) {
            if(instructionText) instructionText.innerText = `Draw Box: ${seatsToMap[currentSeatIdx]}`;
        } else {
            if(instructionText) instructionText.innerText = "Draw Box: All seats mapped! Click Save.";
        }
        drawExistingBoxes();
    });
}

function drawExistingBoxes() {
    const canvases = [
        document.getElementById('calib-canvas'),
        document.getElementById('match-calib-canvas')
    ];
    
    canvases.forEach(c => {
        if(!c) return;
        c.width = c.offsetWidth;
        c.height = c.offsetHeight;
        
        const context = c.getContext('2d');
        if(!context) return;
        context.clearRect(0, 0, c.width, c.height);
        
        context.strokeStyle = "#3b82f6";
        context.lineWidth = 2;
        context.font = "bold 12px monospace";
        context.fillStyle = "#3b82f6";
        
        for (const [seat, coords] of Object.entries(mappingData)) {
            const x = coords[0] * c.width;
            const y = coords[1] * c.height;
            const w = (coords[2] - coords[0]) * c.width;
            const h = (coords[3] - coords[1]) * c.height;
            
            context.strokeRect(x, y, w, h);
            context.fillText(seat, x, y - 5);
        }
    });
}
// FIX: Press 'Escape' to cancel drawing a box
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && isDrawing) {
        isDrawing = false; // Stop the drawing process
        
        // Clear the temporary box off the screen and redraw saved boxes
        drawRectangles(); 
        console.log("Drawing canceled via Escape key.");
    }
});