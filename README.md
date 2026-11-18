# Clip Agent (Mac)

Turn long YouTube videos (even 12-hour streams) into Shorts, TikToks and Reels, for free.
It runs on your own Mac and opens like a website in your browser.

This is a macOS build of [coder677777/clip-agent](https://github.com/coder677777/clip-agent).

- Finds the moments worth posting (hot takes, stories, funny bits, emotional peaks) and scores each one out of 50
- Splits long stories into Part 1 and Part 2, with a cliffhanger ending and a recap line
- Writes the title for the top, the hook for the first seconds, the post caption and hashtags
- Shows where every clip sits on a timeline of the whole video, and lets you preview each moment
- Makes finished 9:16 clips: title band, hook, word-by-word captions in your color, Part labels,
  pauses cut out, audio levelled for phones
- Downloads only the parts it needs. No watermark, no minute limits, no subscription.
- Updates itself every time you open it

## Install (Mac, about 5 minutes, one time)

1. Open **Terminal** (press `Cmd+Space`, type **Terminal**, press Return).
2. Paste this and press **Enter**:

   ```
   curl -fsSL https://raw.githubusercontent.com/jainaarav257-netizen/clip-agent-mac/main/install.sh | bash
   ```

3. Wait for "Done!". It installs Python if you don't have it, sets everything up, and adds a
   **Clip Agent** app to your Desktop and to `~/Applications`, then opens the app.

From then on, just double-click **Clip Agent** on your Desktop (or in `~/Applications`). It runs
quietly in the background and opens at http://127.0.0.1:8765. You can bookmark that.

No Xcode or Homebrew is required — if you already have Python 3.10+ (check with `python3 --version`
in Terminal) the installer uses it as-is. If you don't, and you have [Homebrew](https://brew.sh)
installed, it installs Python for you; otherwise it points you to the official installer at
python.org.

## First time in the app

The app asks for a free Gemini key (the AI that picks the clips):
open https://aistudio.google.com/apikey, sign in, click **Create API key**, copy it, paste it in.

## Making clips

1. Paste a YouTube link, pick how many clips and how long, press **Find clips**.
   You can also use a video file from your computer, or paste a clip plan from Claude.
2. Look through the clips. Each one shows a preview of how it will look, its score, hook,
   first and last words, and the post caption. **Preview the moment** plays it from YouTube.
3. Tick the ones you want, choose a layout and caption color at the bottom, press **Make clips**.
4. Each clip appears in the page when it's done. **Download clip** saves it, and
   **Open clips folder** shows all of them.

Layouts:
- **Blurred background**: any normal 16:9 video (default)
- **Full-screen crop**: one person in the middle of the frame
- **Split screen**: two people side by side (left person on top, right person on the bottom)

## If something goes wrong

- **"Too many requests" from YouTube**: wait 20-30 minutes, or in Settings choose the browser
  you're signed in to YouTube with. If captions stay blocked, the app transcribes the audio itself
  (slower, roughly 10 minutes per hour of video).
- **Reading cookies from Chrome, Edge or Brave asks for your Mac login password**: that's macOS
  Keychain protecting the browser's saved logins, not a bug. Firefox doesn't need a password. Safari
  may need you to grant Terminal (or the Clip Agent app) Full Disk Access in System Settings →
  Privacy & Security.
- **Gemini "free-tier limit"**: the free key has a daily cap. A 12-hour video uses about 12 requests.
- **The page won't open**: double-click the Clip Agent icon again. A log is in
  `~/Library/Application Support/ClipAgent/clip-agent.log`.
- **Reinstall or repair**: run the install command again. Your key and clips are kept.

Clip your own videos, or channels that allow clipping. Reposting other people's content without
permission can get your account struck.

Your clips and settings live in `~/Library/Application Support/ClipAgent`. Fonts: Poppins (SIL Open
Font License).
