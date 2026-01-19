import sys
import asyncio
import json
import struct
import csv
import time
from datetime import datetime
from typing import List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse
from bleak import BleakScanner, BleakClient

# --- CONFIGURATION ---
IMU_SERVICE_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"
IMU_CHAR_UUID    = "0000fff3-0000-1000-8000-00805f9b34fb"
BATT_SERVICE_UUID = "0000180f-0000-1000-8000-00805f9b34fb"
BATT_CHAR_UUID    = "00002a19-0000-1000-8000-00805f9b34fb"

app = FastAPI()

# --- GLOBAL STATE ---
connected_client: BleakClient = None
active_websockets: List[WebSocket] = []
battery_level = 0

# Recording State
is_recording = False
csv_file = None
csv_writer = None

async def notify_callback(sender, data):
    """
    Callback for handling incoming BLE notifications.
    Packet Structure (16 bytes): [Timestamp(4)][AccX(2)][AccY(2)][AccZ(2)][GyroX(2)][GyroY(2)][GyroZ(2)]
    """
    global is_recording, csv_writer

    try:
        # Unpack: I = uint32 (timestamp), h = int16 (6 sensors)
        unpacked = struct.unpack('<Ihhhhhh', data)
        
        timestamp = unpacked[0]
        # Divide by 100.0 to get real Gs and Deg/s
        ax = unpacked[1] / 100.0
        ay = unpacked[2] / 100.0
        az = unpacked[3] / 100.0
        gx = unpacked[4] / 100.0
        gy = unpacked[5] / 100.0
        gz = unpacked[6] / 100.0

        packet = {
            "time": timestamp,
            "ax": ax, "ay": ay, "az": az,
            "gx": gx, "gy": gy, "gz": gz,
            "batt": battery_level 
        }

        # 1. Handle Recording (Write to CSV)
        if is_recording and csv_writer:
            # Columns: Timestamp, AccX, AccY, AccZ, GyroX, GyroY, GyroZ
            csv_writer.writerow([timestamp, ax, ay, az, gx, gy, gz])

        # 2. Broadcast to all connected web clients
        message = json.dumps(packet)
        for ws in active_websockets:
            await ws.send_text(message)
            
    except Exception as e:
        print(f"Error processing packet: {e}")

# --- API ENDPOINTS ---

@app.get("/")
async def get():
    with open("index.html", "r") as f:
        return HTMLResponse(content=f.read(), status_code=200)

@app.get("/scan")
async def scan_devices():
    print("Scanning for devices...")
    devices = await BleakScanner.discover()
    return [{"name": d.name or "Unknown", "address": d.address} for d in devices]

@app.post("/connect/{address}")
async def connect_device(address: str):
    global connected_client, battery_level
    
    if connected_client and connected_client.is_connected:
        await connected_client.disconnect()
        
    try:
        connected_client = BleakClient(address)
        await connected_client.connect()
        print(f"Connected to {address}")

        try:
            batt_data = await connected_client.read_gatt_char(BATT_CHAR_UUID)
            battery_level = int(batt_data[0])
        except Exception as e:
            print(f"Could not read battery: {e}")
            battery_level = -1

        await connected_client.start_notify(IMU_CHAR_UUID, notify_callback)
        
        return {"status": "connected", "battery": battery_level}
    except Exception as e:
        print(f"Connection failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/record/start")
async def start_recording():
    global is_recording, csv_file, csv_writer
    
    if is_recording:
        return {"status": "already_recording"}

    try:
        # Create filename based on current time
        filename = f"imu_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        
        # Open file in write mode with buffering=1 (line buffering)
        csv_file = open(filename, mode='w', newline='')
        csv_writer = csv.writer(csv_file)
        
        # Write Header
        csv_writer.writerow(["Timestamp", "Acc_X", "Acc_Y", "Acc_Z", "Gyro_X", "Gyro_Y", "Gyro_Z"])
        
        is_recording = True
        print(f"Started recording to {filename}")
        return {"status": "started", "filename": filename}
    except Exception as e:
        print(f"Failed to start recording: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/record/stop")
async def stop_recording():
    global is_recording, csv_file, csv_writer
    
    if not is_recording:
        return {"status": "not_recording"}

    try:
        is_recording = False
        if csv_file:
            csv_file.close()
            csv_file = None
            csv_writer = None
        
        print("Stopped recording")
        return {"status": "stopped"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_websockets.append(websocket)
    try:
        while True:
            await websocket.receive_text() 
    except WebSocketDisconnect:
        active_websockets.remove(websocket)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)