import os
import json
import time
import requests
import paho.mqtt.client as mqtt
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import firebase_admin
from firebase_admin import credentials, firestore

# =================================================================
# 1. CONFIGURATION & STATE
# =================================================================
MQTT_BROKER = os.getenv("MQTT_BROKER", "a9fb2e5334c04fb490a9ba3ed3deacde.s1.eu.hivemq.cloud")
MQTT_USER = os.getenv("MQTT_USER", "hivemq.webclient.1775342210787")
MQTT_PASS = os.getenv("MQTT_PASS", "9$cRMnP3@s4CdI,lV5f.")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "GROQ_API_KEY")

from pydantic import BaseModel

TOPIC_TELEMETRY = "farm/+/telemetry"
# Control topic is now dynamic per UID

app = FastAPI()

# Enable CORS for your React Localhost
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize Firebase (Ensure you have your serviceAccountKey.json)
# If deploying to Render, use environment variables to build the credential object
def initialize_firebase():
    # Check if we are running in production (Render) or local
    service_account_info = os.getenv("FIREBASE_SERVICE_ACCOUNT")

    if service_account_info:
        # PRODUCTION: Parse the JSON string from the Environment Variable
        info = json.loads(service_account_info)
        cred = credentials.Certificate(info)
        print("Firebase initialized via Environment Variable.")
    else:
        # LOCAL: Use the local file (only works on your computer)
        try:
            cred = credentials.Certificate("serviceAccountKey.json")
            print("Firebase initialized via local JSON file.")
        except Exception as e:
            print(f"Error: No Firebase credentials found! {e}")
            return None

    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    
    return firestore.client()

db = initialize_firebase()

# In-memory "Fog Cache" with Heartbeat per User
system_states = {}

# =================================================================
# 2. MQTT ORCHESTRATOR
# =================================================================
def on_message(client, userdata, msg):
    try:
        parts = msg.topic.split('/')
        if len(parts) >= 3:
            uid = parts[1]
            data = json.loads(msg.payload.decode())
            if uid not in system_states:
                system_states[uid] = {
                    "telemetry": {
                        "m10": 0, "m30": 0, "m60": 0, 
                        "temp": 0.0, "hum": 0.0, "pump_status": 0,
                        "last_seen": 0
                    },
                    "weather": [],
                    "device_online": False
                }
            system_states[uid]["telemetry"].update(data)
            system_states[uid]["telemetry"]["last_seen"] = time.time()
            print(f"[MQTT] Received Data from ESP32 for {uid}: {data}")
    except Exception as e:
        print(f"MQTT Error: {e}")

mqtt_client = mqtt.Client()
mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
mqtt_client.tls_set()
mqtt_client.on_message = on_message
mqtt_client.connect(MQTT_BROKER, 8883, 60)
mqtt_client.subscribe(TOPIC_TELEMETRY)
mqtt_client.loop_start()

# =================================================================
# 3. LOGIC & DATA FUSION
# =================================================================

def get_device_status(uid: str):
    """Calculates if the ESP32 is actually powered on"""
    if uid not in system_states:
        return False
    # Logic: If last message was > 60 seconds ago, it's offline
    is_online = (time.time() - system_states[uid]["telemetry"]["last_seen"]) < 60
    return is_online

def fetch_3_day_weather():
    """Fetches 3 days of forecast to fix the empty future tiles"""
    # Changed forecast_days to 3
    url = "https://api.open-meteo.com/v1/forecast?latitude=21.17&longitude=79.06&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,et0_fao_evapotranspiration&timezone=Asia%2FKolkata&forecast_days=3"
    try:
        res = requests.get(url).json()
        forecast = []
        for i in range(3):
            forecast.append({
                "date": res["daily"]["time"][i],
                "max_temp": res["daily"]["temperature_2m_max"][i],
                "min_temp": res["daily"]["temperature_2m_min"][i],
                "rain_prob": res["daily"]["precipitation_probability_max"][i],
                "et0": res["daily"]["et0_fao_evapotranspiration"][i]
            })
        return forecast
    except:
        return []

@app.get("/api/system-status")
async def get_full_status(uid: str):
    """React calls this to get Telemetry + Weather + Online Status at once"""
    online = get_device_status(uid)
    weather = fetch_3_day_weather()
    telemetry = system_states[uid]["telemetry"] if uid in system_states else {}
    return {
        "telemetry": telemetry,
        "weather": weather,
        "is_online": online
    }

class AskAIRequest(BaseModel):
    uid: str
    message: str

@app.post("/api/ask-ai")
async def trigger_agentic_decision(req: AskAIRequest):
    """Agentic AI reasoning using Real Firebase Context"""
    uid = req.uid
    
    # 1. Fetch Real User Selections from Firebase
    user_doc = db.collection('users').document(uid).get()
    if not user_doc.exists:
        raise HTTPException(status_code=404, detail="User settings not found in Firebase")
    
    user_settings = user_doc.to_dict()
    weather = fetch_3_day_weather()
    current_sensors = system_states.get(uid, {}).get("telemetry", {})
    
    # 2. Data Fusion Vector
    fusion_context = {
        "sensors": current_sensors,
        "forecast": weather[0] if weather else {}, # Today's forecast
        "farm": user_settings, # Actual Soil/Crop from Firebase
        "user_message": req.message # Pass the user message to LLM
    }

    # 3. AI Reasoning (Prompt matches your ESP32 structure)
    system_prompt = """You are an Agritech AI. Reason based on sensors, weather, and soil type.
    Decide if the pump should turn on. 
    Output ONLY: {"pumpCommand":1,"thresh10":65,"thresh30":60}"""
    
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    payload = {
        "model": "llama3-8b-8192",
        "messages": [{"role": "system", "content": system_prompt},
                     {"role": "user", "content": json.dumps(fusion_context)}],
        "response_format": {"type": "json_object"}
    }

    response = requests.post("https://api.groq.com/openai/v1/chat/completions", json=payload, headers=headers)
    
    if response.status_code == 200:
        decision = json.loads(response.json()["choices"][0]["message"]["content"])
        
        # Save AI decision to Firebase for history tracking
        decision["timestamp"] = firestore.SERVER_TIMESTAMP
        db.collection("users").document(uid).collection("ai_decisions").add(decision)
        
        # 4. Push to ESP32
        topic_control = f"farm/{uid}/control"
        mqtt_client.publish(topic_control, json.dumps(decision))
        return decision
    
    raise HTTPException(status_code=500, detail="AI Brain Error")
