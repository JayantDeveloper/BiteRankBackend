# BiteRank Backend

> Server powering [biterank.tech](https://biterank.tech), aggregating and ranking fast-food deals using AI-driven analysis.

## Overview
The BiteRank backend handles automated data scraping, deal ranking, and API delivery for the BiteRank frontend.  
It collects menus and app-exclusive deals from multiple fast-food chains, then uses **Google Gemini AI** to compute each item’s *value score* based on price, portion size, and nutrition.

Built with **Node.js** and **Python**, the backend merges scraping pipelines with an AI evaluation layer to deliver real-time rankings to the frontend.

---

## Features
- 🍔 **Automated Scraping:** Collects deals and menu data across major chains  
- 🤖 **AI Scoring:** Uses Gemini AI to compute “bang-for-buck” rankings  
- 📡 **REST API:** Serves top-value items to the frontend at [biterank.tech](https://biterank.tech)  
- 🔁 **Scheduled Updates:** Refreshes data and rankings every six hours  

---

## Role in the BiteRank Ecosystem
This repository provides the data engine and API layer that power the BiteRank frontend.  
It connects scraping scripts, AI evaluation, and caching pipelines into a unified backend service.

**Related Repositories**
- [Frontend (Public Interface)](https://github.com/JayantDeveloper/BiteRankFrontend)

Live platform → [biterank.tech](https://biterank.tech)
