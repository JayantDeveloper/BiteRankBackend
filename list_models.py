"""
List all available Gemini models
"""
import google.generativeai as genai
from dotenv import load_dotenv
import os

# Load .env file
load_dotenv()

# Get API key
api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    print("❌ No API key found")
    exit(1)

print(f"✓ API Key found: {api_key[:20]}...")

# Configure Gemini
genai.configure(api_key=api_key)

print("\n📋 Available models:")
print("-" * 50)

try:
    for model in genai.list_models():
        if 'generateContent' in model.supported_generation_methods:
            print(f"✓ {model.name}")
            print(f"  Display name: {model.display_name}")
            print(f"  Description: {model.description[:80]}...")
            print()
except Exception as e:
    print(f"❌ Error listing models: {e}")
    print("\nTrying alternative method...")

    # Try with direct model names
    test_models = [
        'gemini-pro',
        'gemini-1.5-flash',
        'gemini-1.5-pro',
        'gemini-1.0-pro',
        'models/gemini-pro',
        'models/gemini-1.5-flash',
        'models/gemini-1.5-pro',
    ]

    for model_name in test_models:
        try:
            model = genai.GenerativeModel(model_name)
            response = model.generate_content("Hi")
            print(f"✅ WORKING: {model_name}")
        except Exception as e:
            print(f"❌ FAILED: {model_name} - {str(e)[:60]}")
