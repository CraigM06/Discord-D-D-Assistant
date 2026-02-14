import yt_dlp
import discord
import os
import time
from dotenv import load_dotenv
import asyncio
from datetime import datetime
import whisper
import requests
import json

# yt-dlp options for extracting audio
YTDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': False,
    'no_warnings': False,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'opus',
    }],
}

FFMPEG_OPTIONS = {
    'options': '-vn -b:a 128k'
}

# Load the bot token from .env file
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

# Set up the bot with necessary intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = discord.Bot(intents=intents, debug_guilds=[1236353663283495024])

# Global variable to track current song
current_song = {"title": None, "url": None, "url_stream": None, "start_time": None}

# Queue to hold upcoming songs
song_queue = []

# Current position in the queue (index)
queue_position = -1

# History of played songs (for /previous command)
song_history = []

# Recording state
is_recording = False
recording_sink = None
recording_start_time = None

async def play_next(ctx, direction="forward"):
    """Play the next (or previous) song in the queue
    direction can be 'forward', 'backward', or 'jump' (when queue_position is already set)
    """
    global queue_position
    
    if len(song_queue) == 0:
        print("Queue is empty")
        return
    
    # Add current song to history if moving forward
    if direction == "forward" and current_song["title"] is not None:
        song_history.append({
            'url': current_song["url_stream"],
            'title': current_song['title'],
            'webpage_url': current_song['url']
        })
    
    # Determine which song to play
    if direction == "forward":
        queue_position += 1
    elif direction == "backward":
        queue_position -= 1
    # If direction is "jump", queue_position is already set
    
    # Check bounds
    if queue_position < 0:
        queue_position = 0
    if queue_position >= len(song_queue):
        print("Reached end of queue")
        queue_position = len(song_queue) - 1
        return
    
    next_song = song_queue[queue_position]
    
    print(f"Playing position {queue_position + 1}: {next_song['title']}")
    
    # Store current song info
    current_song["title"] = next_song['title']
    current_song["url"] = next_song['webpage_url']
    current_song["url_stream"] = next_song['url']
    current_song["start_time"] = time.time()
    
    # Play the audio
    source = discord.FFmpegPCMAudio(next_song['url'], **FFMPEG_OPTIONS)
    
    def after_playing(error):
        finished_title = current_song['title']
        
        if error:
            print(f"Player error: {error}")
        else:
            print(f"Finished playing: {finished_title}")
            
        # Play next song in queue if available
        import asyncio
        if queue_position < len(song_queue) - 1 and ctx.voice_client:
            asyncio.run_coroutine_threadsafe(play_next(ctx, "forward"), bot.loop)
        else:
            # Clear current song when queue ends
            current_song["title"] = None
            current_song["url"] = None
            current_song["url_stream"] = None
            current_song["start_time"] = None
    
    ctx.voice_client.play(source, after=after_playing)
    
async def process_recording(ctx, audio_data, start_time):
    """Process recorded audio: combine, transcribe, and summarize"""
    try:
        from pydub import AudioSegment
        import io
        
        print(f"Processing recording with {len(audio_data)} audio streams")
        print(f"User IDs in recording: {list(audio_data.keys())}")
        
        # Combine all audio streams
        combined_audio = None
        for user_id, audio in audio_data.items():
            print(f"Processing audio from user {user_id}")
            audio.file.seek(0)
            # Try to detect format automatically
            try:
                audio_segment = AudioSegment.from_file(audio.file, format="mp3")
            except:
                audio.file.seek(0)
                audio_segment = AudioSegment.from_file(audio.file, format="wav")
            print(f"Audio segment duration: {len(audio_segment)}ms")
            
            if combined_audio is None:
                combined_audio = audio_segment
            else:
                # Overlay all voices together
                combined_audio = combined_audio.overlay(audio_segment)
        
        if combined_audio is None:
            await ctx.send("❌ No audio to process!")
            return
        
        # Save combined audio temporarily
        timestamp = start_time.strftime("%Y%m%d_%H%M%S")
        temp_file = f"recording_{timestamp}.mp3"
        combined_audio.export(temp_file, format="mp3")
        
        await ctx.send("🎧 Audio combined. Starting transcription... (this will take a while)")

        # Convert MP3 to WAV for Whisper (it works better with WAV)
        wav_file = temp_file.replace('.mp3', '.wav')
        combined_audio.export(wav_file, format="wav")
        
        print(f"Exported audio file: {wav_file}")
        print(f"File size: {os.path.getsize(wav_file)} bytes")
        
        # Transcribe using Whisper
        model = whisper.load_model("base")
        print("Whisper model loaded, starting transcription...")
        result = model.transcribe(wav_file)
        
        transcript = result["text"]
        print(f"Transcript length: {len(transcript)} characters")
        print(f"Transcript preview: {transcript[:200]}")
        
        await ctx.send("✅ Transcription complete! Generating summary...")
        
        print(f"Sending transcript to Ollama: {transcript}")
        
        # Summarize using Ollama
        summary_prompt = f"""You are summarizing a D&D session transcript. Extract only the KEY EVENTS and DECISIONS.

Transcript:
{transcript}

Provide a concise summary with:
1. Major story events that happened
2. Important decisions the party made
3. Key NPCs encountered
4. Loot or rewards obtained
5. Next session hooks/cliffhangers

Keep it brief - focus only on what matters for continuity."""

        # Call Ollama API
        response = requests.post(
            'http://localhost:11434/api/generate',
            json={
                'model': 'llama3.2',
                'prompt': summary_prompt,
                'stream': False
            }
        )
        
        summary = response.json()['response']
        
        # Format the output
        session_date = start_time.strftime("%B %d, %Y at %I:%M %p")
        output = f"""# D&D Session Summary
**Date:** {session_date}

## Key Events & Decisions
{summary}

---
*Full transcript available upon request*
"""
        
        # Send to Discord (we'll add Google Sheets option later)
        message = await ctx.send(output)
        await message.pin()
        
        await ctx.send("📌 Session summary has been pinned!")
        
        # Clean up temp file
        os.remove(temp_file)
        os.remove(wav_file)
        
    except Exception as e:
        await ctx.send(f"❌ Error processing recording: {str(e)}")
        print(f"Processing error: {e}")

@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')

@bot.slash_command(name="hello", description="Test command")
async def hello(ctx):
    await ctx.respond('Hello! DnD Session Assistant is online!')

@bot.slash_command(name="join", description="Join your voice channel")
async def join(ctx):
    if ctx.author.voice is None:
        await ctx.respond("You need to be in a voice channel!")
        return
    
    voice_channel = ctx.author.voice.channel
    if ctx.voice_client is None:
        await voice_channel.connect()
        await ctx.respond(f"Joined {voice_channel.name}!")
    else:
        await ctx.respond("I'm already in a voice channel!")

@bot.slash_command(name="leave", description="Leave the voice channel")
async def leave(ctx):
    if ctx.voice_client is None:
        await ctx.respond("I'm not in a voice channel!")
        return
    
    await ctx.voice_client.disconnect()
    await ctx.respond("Left the voice channel!")
    
@bot.slash_command(name="testplay", description="Test audio with a simple tone")
async def testplay(ctx):
    if ctx.author.voice is None:
        await ctx.respond("You need to be in a voice channel!")
        return
    
    if ctx.voice_client is None:
        voice_channel = ctx.author.voice.channel
        await voice_channel.connect()
    
    # Generate a simple test tone using FFmpeg
    source = discord.FFmpegPCMAudio('sine=frequency=1000:duration=5', 
    source='lavfi',
    options='-f lavfi')
    
    def after_playing(error):
        if error:
            print(f"Test player error: {error}")
        else:
            print("Test tone finished playing")
    
    ctx.voice_client.play(source, after=after_playing)
    await ctx.respond("🔊 Playing 5-second test tone...")
    
@bot.slash_command(name="play", description="Play audio from a YouTube URL")
async def play(ctx, url: str):
    # Check if user is in voice channel
    if ctx.author.voice is None:
        await ctx.respond("You need to be in a voice channel!")
        return
    
    # Join voice channel if not already connected
    if ctx.voice_client is None:
        voice_channel = ctx.author.voice.channel
        await voice_channel.connect()
    
    # Stop current audio if playing
    if ctx.voice_client.is_playing():
        ctx.voice_client.stop()
    
    await ctx.respond(f"🎵 Loading audio from: {url}")
    
    # Extract audio info
    with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
        try:
            print(f"Extracting info from: {url}")
            info = ydl.extract_info(url, download=False)
            url2 = info['url']
            title = info.get('title', 'Unknown')
            
            print(f"Playing: {title}")
            print(f"Stream URL: {url2[:100]}...")  # Print first 100 chars of stream URL
            
            # Store current song info
            current_song["title"] = title
            current_song["url"] = url
            current_song["url_stream"] = url2
            current_song["start_time"] = time.time()
            
            # Play the audio
            source = discord.FFmpegPCMAudio(url2, **FFMPEG_OPTIONS)
            
            def after_playing(error):
                # Capture the title before clearing
                finished_title = current_song['title']
                
                # Clear current song info when done
                current_song["title"] = None
                current_song["url"] = None
                current_song["url_stream"] = None
                current_song["start_time"] = None
                
                if error:
                    print(f"Player error: {error}")
                else:
                    print(f"Finished playing: {finished_title}")
            
            ctx.voice_client.play(source, after=after_playing)
            
            await ctx.respond(f"▶️ Now playing: **{title}**")
        except Exception as e:
            print(f"Error: {e}")
            await ctx.respond(f"❌ Error playing audio: {str(e)}")
            
@bot.slash_command(name="queue", description="Add a song to the queue")
async def queue_song(ctx, url: str):
    await ctx.respond(f"🔍 Adding to queue: {url}")
    
    # Extract song info
    with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            song_info = {
                'url': info['url'],
                'title': info.get('title', 'Unknown'),
                'webpage_url': url
            }
            song_queue.append(song_info)
            
            position = len(song_queue)
            await ctx.respond(f"✅ Added to queue (#{position}): **{song_info['title']}**")
            
            # If nothing is playing, start playing
            if ctx.voice_client and not ctx.voice_client.is_playing():
                global queue_position
                if len(song_queue) == 1:  # First song added
                    queue_position = -1  # Will become 0 when play_next increments
                await play_next(ctx, "forward")
                
        except Exception as e:
            print(f"Error adding to queue: {e}")
            await ctx.respond(f"❌ Error adding to queue: {str(e)}")
            
@bot.slash_command(name="playlist", description="Add all songs from a YouTube/YouTube Music playlist to queue")
async def playlist(ctx, url: str):
    await ctx.respond(f"🔍 Fetching playlist from: {url}")
    
    # Special options for playlists
    playlist_opts = YTDL_OPTIONS.copy()
    playlist_opts['noplaylist'] = False
    playlist_opts['extract_flat'] = True  # Don't download, just get URLs
    
    with yt_dlp.YoutubeDL(playlist_opts) as ydl:
        try:
            print(f"Extracting playlist info from: {url}")
            playlist_info = ydl.extract_info(url, download=False)
            
            if 'entries' not in playlist_info:
                await ctx.respond("❌ This doesn't appear to be a playlist!")
                return
            
            entries = playlist_info['entries']
            playlist_title = playlist_info.get('title', 'Unknown Playlist')
            
            await ctx.respond(f"📋 Found playlist: **{playlist_title}** ({len(entries)} songs)\n⏳ Adding to queue...")
            
            added_count = 0
            for entry in entries:
                if entry is None:
                    continue
                
                try:
                    # Get full info for each song
                    video_url = f"https://www.youtube.com/watch?v={entry['id']}"
                    song_info_detailed = ydl.extract_info(video_url, download=False)
                    
                    song_info = {
                        'url': song_info_detailed['url'],
                        'title': song_info_detailed.get('title', 'Unknown'),
                        'webpage_url': video_url
                    }
                    song_queue.append(song_info)
                    added_count += 1
                    print(f"Added to queue: {song_info['title']}")
                    
                except Exception as e:
                    print(f"Skipped a song due to error: {e}")
                    continue
            
            await ctx.respond(f"✅ Added {added_count} songs from **{playlist_title}** to the queue!")
            
            # If not in voice, just notify user
            if ctx.voice_client is None:
                await ctx.respond("💡 Use `/join` to have the bot auto-play, or it will start when you manually join it to a voice channel!")
                return

            # If nothing is playing, start playing
            if not ctx.voice_client.is_playing():
                await play_next(ctx)
                
        except Exception as e:
            print(f"Error loading playlist: {e}")
            await ctx.respond(f"❌ Error loading playlist: {str(e)}")
            
@bot.slash_command(name="playnum", description="Play a specific song from the queue by number")
async def playnum(ctx, number: int):
    if ctx.voice_client is None:
        await ctx.respond("I'm not in a voice channel!")
        return
    
    if len(song_queue) == 0:
        await ctx.respond("Queue is empty!")
        return
    
    if number < 1 or number > len(song_queue):
        await ctx.respond(f"Invalid number! Queue has {len(song_queue)} songs.")
        return
    
    global queue_position
    queue_position = number - 2  # Will be incremented to number-1 by play_next
    
    # Stop current playback
    if ctx.voice_client.is_playing():
        ctx.voice_client.stop()
    
    # Jump to selected position
    queue_position = number - 1  # Direct set for jump
    await play_next(ctx, "jump")
    
    await ctx.respond(f"🎵 Jumping to #{number}: **{song_queue[number-1]['title']}**")
            
@bot.slash_command(name="showqueue", description="Show the current song queue")
async def showqueue(ctx):
    if len(song_queue) == 0:
        await ctx.respond("Queue is empty!")
        return
    
    message = f"📋 **Queue ({len(song_queue)} song(s)):**\n\n"
    
    for i, song in enumerate(song_queue):
        # Highlight currently playing song
        if i == queue_position and current_song["title"]:
            elapsed = int(time.time() - current_song["start_time"])
            minutes = elapsed // 60
            seconds = elapsed % 60
            message += f"▶️ **{i + 1}. {song['title']}** ({minutes}:{seconds:02d})\n"
        else:
            message += f"{i + 1}. {song['title']}\n"
    
    await ctx.respond(message)
    
@bot.slash_command(name="skip", description="Skip to the next song")
async def skip(ctx):
    if ctx.voice_client is None:
        await ctx.respond("I'm not in a voice channel!")
        return
    
    if not ctx.voice_client.is_playing():
        await ctx.respond("Nothing is playing!")
        return
    
    if queue_position >= len(song_queue) - 1:
        await ctx.respond("This is the last song in the queue!")
        return
    
    skipped_title = current_song["title"]
    ctx.voice_client.stop()  # Will trigger after_playing which plays next
    await ctx.respond(f"⏭️ Skipped: **{skipped_title}**")
    
@bot.slash_command(name="previous", description="Play the previous song")
async def previous(ctx):
    global queue_position  # Move this to the TOP of the function
    
    if ctx.voice_client is None:
        await ctx.respond("I'm not in a voice channel!")
        return
    
    if queue_position <= 0:
        await ctx.respond("This is the first song in the queue!")
        return
    
    # Calculate the target position
    target_position = queue_position - 1
    prev_song_title = song_queue[target_position]['title']
    
    # Set position so play_next("forward") lands on target
    queue_position = target_position - 1
    
    if ctx.voice_client.is_playing():
        ctx.voice_client.stop()
    
    await play_next(ctx, "forward")
    await ctx.respond(f"⏮️ Playing previous: **{prev_song_title}**")
    
@bot.slash_command(name="clearqueue", description="Clear all songs from the queue")
async def clearqueue(ctx):
    if len(song_queue) == 0:
        await ctx.respond("Queue is already empty!")
        return
    
    cleared_count = len(song_queue)
    song_queue.clear()
    await ctx.respond(f"🗑️ Cleared {cleared_count} song(s) from the queue")
            
@bot.slash_command(name="stop", description="Stop playback and clear the queue")
async def stop(ctx):
    if ctx.voice_client is None:
        await ctx.respond("I'm not in a voice channel!")
        return
    
    if not ctx.voice_client.is_playing() and len(song_queue) == 0:
        await ctx.respond("Nothing is playing and queue is empty!")
        return
    
    # Stop playback
    if ctx.voice_client.is_playing():
        ctx.voice_client.stop()
    
    # Clear everything
    cleared_count = len(song_queue)
    song_queue.clear()
    current_song["title"] = None
    current_song["url"] = None
    current_song["url_stream"] = None
    current_song["start_time"] = None
    
    await ctx.respond(f"⏹️ Stopped playback and cleared {cleared_count} song(s) from queue")

@bot.slash_command(name="nowplaying", description="Show what's currently playing")
async def nowplaying(ctx):

    if ctx.voice_client is None:
        await ctx.respond("I'm not in a voice channel!")
        return
    
    if ctx.voice_client.is_playing() and current_song["title"]:
        import time
        elapsed = int(time.time() - current_song["start_time"])
        minutes = elapsed // 60
        seconds = elapsed % 60
        
        await ctx.respond(
            f"🎵 **Now Playing:**\n"
            f"**{current_song['title']}**\n"
            f"⏱️ Playing for: {minutes}:{seconds:02d}"
        )
    else:
        await ctx.respond("Nothing is currently playing!")
        
@bot.slash_command(name="startrecording", description="Start recording the voice channel")
async def startrecording(ctx):
    global is_recording, recording_sink, recording_start_time
    
    if ctx.voice_client is None:
        await ctx.respond("I need to be in a voice channel to record! Use `/join` first.")
        return
    
    if is_recording:
        await ctx.respond("Already recording!")
        return
    
    # Try MP3Sink instead of WaveSink
    recording_sink = discord.sinks.MP3Sink()
    
    # Async callback for when recording stops
    async def finished_callback(sink, *args):
        print("Recording finished callback triggered")
    
    # Start recording
    ctx.voice_client.start_recording(
        recording_sink,
        finished_callback,
        ctx
    )
    
    is_recording = True
    recording_start_time = datetime.now()
    
    await ctx.respond("🔴 **Recording started!** Use `/stoprecording` when done.")
    print(f"Recording started at {recording_start_time}")
    
@bot.slash_command(name="stoprecording", description="Stop recording and process the audio")
async def stoprecording(ctx):
    global is_recording, recording_sink, recording_start_time
    
    if not is_recording:
        await ctx.respond("Not currently recording!")
        return
    
    await ctx.respond("⏹️ Stopping recording... Please wait while I process the audio.")
    
    # Stop recording
    ctx.voice_client.stop_recording()
    is_recording = False
    
    # Get the recorded audio files
    audio_data = recording_sink.audio_data
    
    if not audio_data:
        await ctx.respond("❌ No audio was recorded!")
        return
    
    await ctx.respond(f"📝 Processing audio from {len(audio_data)} speaker(s)... This may take a few minutes.")
    
    # Process the recording in a separate thread to avoid blocking
    asyncio.create_task(process_recording(ctx, audio_data, recording_start_time))

# Run the bot
bot.run(TOKEN)