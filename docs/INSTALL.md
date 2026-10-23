# Installing Story Loom

This guide starts from zero. Follow the steps in order. Each step says
what to do and what you should see when it worked.

**You need:** a Windows PC with an NVIDIA graphics card that has 24 GB of
memory, an internet connection, and about 40 GB of free disk space to
store the model itself.

← [Back to the README](../README.md) · Next: [Basic usage](USAGE.md)

---

## Part A — Install the tools (once)

### Step 1. Update your NVIDIA driver

1. Go to <https://www.nvidia.com/Download/index.aspx>.
2. Choose your graphics card, download the driver, and run the installer
   with the default choices.
3. Restart the computer when it asks.

✔ Done when the installer says it finished.

### Step 2. Install Python 3.12

1. Go to <https://www.python.org/downloads/windows/>.
2. Under **Python 3.12**, download the **Windows installer (64-bit)**.
3. Run it. On the first screen, tick **Add python.exe to PATH**, then
   click **Install Now**.

✔ Done when the installer says "Setup was successful".

### Step 3. Install Git for Windows

This gives you **Git Bash**, the window you will start Story Loom from.

1. Go to <https://git-scm.com/download/win> and download the installer.
2. Run it and click **Next** on every screen, then **Install**.

✔ Done when the installer finishes.

---

## Part B — Get Story Loom and its parts (once)

### Step 4. Download Story Loom

1. Open the Story Loom page on GitHub.
2. Click the green **Code** button, then **Download ZIP**.
3. Open your Downloads folder, right-click the zip, choose
   **Extract All…**, and extract it to `C:\StoryLoom`.

✔ Done when `C:\StoryLoom` contains a file called `start.sh`.

In the rest of this guide, `C:\StoryLoom` is called **the app folder**.

### Step 5. Make two folders

In the app folder, make two new, empty folders:

- `models`
- `llama`

(Right-click in the folder → **New** → **Folder**.)

### Step 6. Download the model

The model is the AI that writes. Story Loom uses **one model for
everything**, plot and prose: **Cydonia 24B v4.1**.

1. Go to <https://huggingface.co/bartowski/TheDrummer_Cydonia-24B-v4.1-GGUF/tree/main>.
2. In the file list, find **`TheDrummer_Cydonia-24B-v4.1-Q5_K_M.gguf`**
   (about 16.8 GB). Click the small download arrow next to it.
3. When the download has finished, move the file into the app folder's
   **`models`** folder. Do not rename it.

✔ Done when you have
`C:\StoryLoom\models\TheDrummer_Cydonia-24B-v4.1-Q5_K_M.gguf`.

The download is large; it can take an hour or more.

### Step 7. Download llama.cpp

llama.cpp is the program that runs the model on your graphics card.

1. Go to <https://github.com/ggml-org/llama.cpp/releases/latest>.
2. Under **Assets**, download the TWO zip files whose names contain
   **`win-cuda`** and **`x64`**:
   - one starting with **`llama-`**
   - one starting with **`cudart-`**

   If there are several CUDA versions (for example `12.4` and `13.1`),
   pick the same version for both files.
3. Extract BOTH zips into the app folder's **`llama`** folder
   (right-click → **Extract All…** → choose `C:\StoryLoom\llama`).

✔ Done when you have `C:\StoryLoom\llama\llama-server.exe`.

---

## Part C — Start and stop

### Step 8. Start Story Loom

1. Open the app folder in File Explorer.
2. Right-click an empty spot in the folder and choose
   **Open Git Bash here**. (On Windows 11, choose **Show more options**
   first.) A black window opens.
3. Type this and press Enter:

   ```
   ./start.sh
   ```

4. Wait. Lines of text scroll past.

✔ Done when the window says **ready**. Leave this window open — closing
it does not stop Story Loom, but you need it to stop it later.

Your book is saved inside the app folder.

### Step 9. Open Story Loom in your browser

Open this address in Chrome, Edge or Firefox:

<http://127.0.0.1:8888/index.html>

✔ Done when you see the Story Loom page with **Writing** at the top.

You are installed. Continue with [Basic usage](USAGE.md).

### Stopping Story Loom

In the Git Bash window, type this and press Enter:

```
./start.sh --stop
```

This also frees your graphics card. Next time, start again at Step 8.

---

## If something goes wrong

**"model file not found"** — the model file is not in the `models`
folder, or its name was changed. Check Step 6.

**"could not run llama-server"** — llama.cpp is not in the `llama`
folder. Check Step 7: `llama-server.exe` must be directly inside
`C:\StoryLoom\llama`, not in a folder inside it.

**"not enough free VRAM"** — something else is using your graphics card
(a game, another AI program). Close it and try again.

**The page does not open** — Story Loom is not running. Do Step 8 again
and wait for **ready**.

---

## Already have the files somewhere else?

You can tell Story Loom where they are instead of copying them:

```
./start.sh --models "D:/my-models" --llama "D:/llama/llama-server.exe"
```

And to keep a book in its own folder instead of the app folder:

```
./start.sh "D:/Books/My First Book"
```
