# MP4 Splitter

[繁體中文版](README_zh-TW.md)

A lightweight, web-based tool built with Python and Flask to split MP4 videos into smaller clips of a specified duration. It features a drag-and-drop interface, real-time progress tracking, and specialized export options for video editors.
![Screenshot](./images/01.png)
![Screenshot](./images/02.png)

## Features

- **Drag-and-Drop Upload:** Easily select your MP4 files.
- **Custom Split Duration:** Define the exact length (in seconds) for each clip.
- **ProRes 422 Conversion:** Option to convert split clips into high-quality ProRes 422 (MOV) format, ideal for Final Cut Pro and professional editing workflows.
- **Web Previews:** Automatically generates lightweight MP4 proxy clips so you can preview your ProRes masters directly in the browser.
- **ZIP Export:** Download all your split clips at once in a convenient ZIP archive.
- **Real-time Progress:** Live progress bar and status updates during the splitting/transcoding process.
- **Auto-cleanup:** Automatically manages and cleans up temporary files and old jobs to save disk space.
![Screenshot](./images/03.png)
## Prerequisites

- **Python 3.8+**
- **FFmpeg:** You must have `ffmpeg` installed and added to your system's PATH. 
  - *Windows:* Download from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) or install via winget: `winget install ffmpeg`
  - *macOS:* `brew install ffmpeg`
  - *Linux:* `sudo apt install ffmpeg`

## Installation

1. **Clone or download the repository:**
   ```bash
   git clone <your-repo-url>
   cd mp4_split
   ```

2. **Create a Python virtual environment:**
   ```bash
   python -m venv .venv
   ```

3. **Activate the virtual environment:**
   - **Windows:** `.\.venv\Scripts\activate`
   - **macOS/Linux:** `source .venv/bin/activate`

4. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

## Usage

1. **Start the local server:**
   ```bash
   python app.py
   ```
2. **Open your browser** and navigate to `http://127.0.0.1:5000`.
3. **Upload an MP4 file** by dragging it into the drop zone or clicking to browse.
4. Configure your split options (see below) and click **"Split & Preview"**.
5. Keep the browser tab open while it processes. Once complete, you can preview the clips and download them individually or hit **"Download ZIP"**.

## Processing Options

- **Clip duration (seconds):** Determines the length of each output clip. For example, a 180-second video with a 30-second duration will yield 6 clips.
- **Convert to ProRes 422 (MOV):**
  - *Checked (Default):* Converts the split master files into ProRes 422 `.mov` format. It simultaneously generates smaller H.264 `.mp4` proxies so you can still preview the clips natively in your web browser. Highly recommended if you are bringing these clips into an NLE like Final Cut Pro.
  - *Unchecked:* Re-encodes the split segments into standard H.264 `.mp4` files. This ensures accurate cut points while maintaining standard MP4 playback compatibility everywhere.
