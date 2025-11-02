"""
Quick test script to verify Gemini API key works
Run: python test_gemini.py
"""
import google.generativeai as genai
from dotenv import load_dotenv
import os

# Load .env file
load_dotenv()

# Get API key
api_key = os.getenv("GEMINI_API_KEY")

if not api_key or api_key == "your_gemini_api_key_here":
    print("❌ ERROR: GEMINI_API_KEY not set in .env file!")
    print("Please edit backend/.env and add your real API key")
    exit(1)

print(f"✓ API Key found: {api_key[:20]}...")

# Configure Gemini
genai.configure(api_key=api_key)

# Test API call
try:
    model = genai.GenerativeModel('gemini-2.5-flash')
    response = model.generate_content("Say 'Hello, MenuRanker!' in one sentence.")
    print(f"✓ Gemini responded: {response.text}")
    print("\n✅ SUCCESS! Your Gemini API key is working!")
    print("You can now use AI ranking in MenuRanker!")
except Exception as e:
    print(f"❌ ERROR: {e}")
    print("\nPossible issues:")
    print("1. Invalid API key")
    print("2. No internet connection")
    print("3. Gemini API is down")
    print("\nCheck your API key at: https://ai.google.dev/")
