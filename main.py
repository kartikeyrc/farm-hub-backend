import os
import json
import requests
import paho.mqtt.client as mqtt
from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel
import firebase_admin
from firebase_admin import credentials, firestore

# =================================================================
# 1. CONFIGURATION & CREDENTIALS
# =================================================================
# Load from Environment Variables for security during deployment
MQTT_BROKER = os.getenv("MQTT_BROKER", "YOUR_HIVEMQ_URL.s1.eu.hivemq.cloud")
MQTT_PORT = 8883
MQTT_USER = os.getenv("MQTT_USER", "farm_admin")
MQTT_PASS = os.getenv("MQTT_PASS", "your_password")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "YOUR_GROQ_API_KEY")

TOPIC_TELEMETRY = "farm/user123/telemetry"
TOPIC_CONTROL = "farm/user123/control"

# Global state to hold the latest ESP32 data (The "Fog" Cache)
latest_telemetry = {
    "m10": 0, "m30": 0, "m60": 0, 
    "temp": 0.0, "hum": 0.0, "pump_status": 0
}

app = FastAPI(title="Agro-Agent Cognitive Core")

# Initialize Firebase (Requires your service account JSON file)
# cred = credentials.Certificate("firebase-adminsdk.json")
# firebase_admin.initialize_app(cred)
# db = firestore.client()

# =================================================================
# 2. MQTT BACKGROUND THREAD (Perception Listener)
# =================================================================
def on_connect(client, userdata, flags, rc):
    print(f"Connected to HiveMQ with result code {rc}")
    client.subscribe(TOPIC_TELEMETRY)

def on_message(client, userdata, msg):
    global latest_telemetry
    try:
        payload = json.loads(msg.payload.decode())
        latest_telemetry.update(payload)
        print(f"[MQTT] Telemetry Updated: {latest_telemetry}")
    except Exception as e:
        print(f"Error parsing MQTT: {e}")

mqtt_client = mqtt.Client(client_id="Agro-Python-Backend")
mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
mqtt_client.tls_set() # Enable secure connection for Port 8883
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message

# Start MQTT loop in the background
mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
mqtt_client.loop_start()

# =================================================================
# 3. DATA FUSION & API LOGIC
# =================================================================
def fetch_weather_data():
    """Fetches Rain Probability and ET0 from Open-Meteo"""
    url = "https://api.open-meteo.com/v1/forecast?latitude=21.1768645&longitude=79.0606934&daily=precipitation_probability_max,et0_fao_evapotranspiration&timezone=Asia%2FKolkata&forecast_days=1"
    try:
        res = requests.get(url)
        data = res.json()
        return {
            "rainProb": data["daily"]["precipitation_probability_max"][0],
            "evapotranspiration": data["daily"]["et0_fao_evapotranspiration"][0]
        }
    except Exception as e:
        print(f"Weather API Error: {e}")
        return {"rainProb": 0, "evapotranspiration": 0.0}

def fetch_firebase_context(user_id: str):
    """Mocks fetching Soil, Crop, and Sow Date from Firebase"""
    # In production: doc = db.collection('farms').document(user_id).get()
    return {
        "soil_type": "Black Cotton Soil",
        "crop_type": "Soybean",
        "days_since_sow": 45
    }

@app.post("/api/ask-ai")
async def trigger_ai_watering_decision():
    """
    The main Agentic Data Fusion Endpoint. 
    Triggered by React Dashboard 'Should I Water?' button.
    """
    # 1. Gather Data Vector
    weather = fetch_weather_data()
    farm_context = fetch_firebase_context("user123")
    
    # Mathematical Data Fusion
    fusion_state = {**latest_telemetry, **weather, **farm_context}
    
    # 2. Construct Prompt for Groq
    system_prompt = """You are an Agritech AI. Evaluate the provided environmental and crop data. 
    Decide if the pump should turn on (1) or stay off (0). 
    Calculate target moisture thresholds (0-100) for 10cm and 30cm depth. 
    Output ONLY a raw, minified JSON object: {"pumpCommand":1,"thresh10":65,"thresh30":60}"""
    
    payload = {
        "model": "llama3-8b-8192", # Update to valid Groq model
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(fusion_state)}
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"}
    }
    
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # 3. Call Groq
    response = requests.post("https://api.groq.com/openai/v1/chat/completions", json=payload, headers=headers)
    
    if response.status_code == 200:
        ai_output = response.json()["choices"][0]["message"]["content"]
        command_json = json.loads(ai_output)
        
        # 4. Publish Command to ESP32 via MQTT
        mqtt_client.publish(TOPIC_CONTROL, json.dumps(command_json))
        
        return {
            "status": "success", 
            "fusion_data": fusion_state, 
            "ai_decision": command_json
        }
    else:
        raise HTTPException(status_code=500, detail="Groq API Failed")

# Run locally using: uvicorn main:app --reload