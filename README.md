# Discord Music Bot

A **modular Discord music bot** written in Python using `discord.py`.  
This bot supports music playback, queue and playlist management, and more.

## Features

- Play music from supported sources, also with a normal search, instead of a link
- Queue, history, and playlist management
- Skip, pause, resume, and stop playback
- Autoplay mode using Last.fm (similar tracks, artist top tracks, similar artists)
- Modular command system

# Configuration

This bot uses **environment variables** for secrets:

1. Create a `.env` file in the root directory.
2. Add your Discord token:
    - BOT_TOKEN=your_token

3. For spotify, you will have to go to the website and get the spotify client id and secret, then put in the .env
   like this:
    - SPOTIFY_CLIENT_ID=client_id
    - SPOTIFY_CLIENT_SECRET=client_secret

4. For Last.fm (required for autoplay feature), get your API credentials from https://www.last.fm/api/account/create
   and add them to the .env:
    - LASTFM_API_KEY=your_api_key
    - LASTFM_API_SECRET=your_api_secret

5. Make sure `.env` remains in `.gitignore` so it is **never committed**.

# How to run

1. ffmpeg must be installed on your PC. Download from -> https://www.ffmpeg.org/download.html
2. Install the libraries in requirements.txt by doing `pip install -r requirements.txt`
3. Execute main.py

# License

This bot is only for personal use
