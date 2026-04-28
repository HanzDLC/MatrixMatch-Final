# MatrixMatch — Complete Setup Guide

This guide will walk you through setting up MatrixMatch from scratch on a new Windows PC.

## Step 1: Install Required Software

You need three foundational tools before configuring the project:

### 1. XAMPP (Local Web & Database Server)
1. Download XAMPP for Windows from [Apache Friends](https://www.apachefriends.org/).
2. Run the installer (you can accept all default settings).
3. Open the **XAMPP Control Panel**.
4. Click **Start** next to both **Apache** and **MySQL**.

### 2. Miniconda (Python Environment Manager)
1. Download the Miniconda installer for Windows from the [official site](https://docs.conda.io/en/latest/miniconda.html).
2. Run the installer. 
3. **Important during install:** It's recommended to install for "Just Me" and you do *not* need to add it to your PATH variable.

### 3. PyCharm (Code Editor)
1. Download PyCharm Community Edition (which is free) from [JetBrains](https://www.jetbrains.com/pycharm/download/?section=windows).
2. Run the installer and complete the setup.

---

## Step 2: Database Setup

1. Make sure **MySQL** and **Apache** are running in your XAMPP Control Panel.
2. Open your web browser and go to `http://localhost/phpmyadmin/`.
3. Click **Databases** at the top and create a new database named `matrixmatch`.
4. Click on your new `matrixmatch` database on the left sidebar.
5. Go to the **Import** tab at the top.
6. Click **Choose File** and select the `matrixmatch.sql` file located in your project folder.
7. Scroll down and click **Import** (or **Go**).

---

## Step 3: Set Up the Python Environment

We will use Miniconda to create an isolated "virtual environment" so the app's packages don't interfere with your system.

1. Open **Anaconda Prompt** from your Windows Start Menu.
2. Navigate to your MatrixMatch project folder (using the `cd` command):
   ```bash
   cd path\to\FINAL VERSION MATRIXMATCH\MatrixMatch
   ```
3. Create the Python environment by typing:
   ```bash
   conda create -n matrixmatch python=3.11 -y
   ```
4. Activate the environment:
   ```bash
   conda activate matrixmatch
   ```
   *Your prompt should now be prefixed with `(matrixmatch)` instead of `(base)`.*

---

## Step 4: Install Packages

While inside the `(matrixmatch)` environment in Anaconda Prompt, run the following command to download all required packages:

```bash
pip install flask mysql-connector-python python-dotenv sentence-transformers rank-bm25 pandas matplotlib requests openai google-genai
```
*(Note: Expect this to take 5–10 minutes depending on your internet speed, as `sentence-transformers` downloads large AI libraries like PyTorch).*

**What each package is for:**
- `flask` — Web framework for the app's routing.
- `mysql-connector-python` — Connects the app to your XAMPP MySQL database.
- `python-dotenv` — Loads configuration from the `.env` file.
- `sentence-transformers` — SBERT AI model for comparing text similarity.
- `rank-bm25` — BM25 keyword matching algorithm.
- `pandas` & `matplotlib` — Tools for manipulating data and rendering heatmaps.
- `requests` — HTTP client (used for local Ollama LLM queries).
- `openai` / `google-genai` — SDKs for interacting with external AI providers for the Generative Gap Analysis.

---

## Step 5: Configure the Application

1. In your project folder, make sure there is a file named `.env`. 
2. Open `.env` in any text editor and ensure the database settings match your local XAMPP setup (by default in XAMPP, the username is usually `root` with a blank password):
   ```ini
   DB_HOST=127.0.0.1
   DB_USER=root
   DB_PASS=
   DB_NAME=matrixmatch
   ```
3. *(Optional)* Add any required API keys to this `.env` file if you plan on using the OpenAI or Google Gemini LLM providers.

---

## Step 6: PyCharm Configuration (Choosing the Conda Environment)

To make PyCharm recognize your code correctly and help you run the app without errors, you must tell it to use the `matrixmatch` Conda environment.

1. Open **PyCharm** and select **Open**. Navigate to and select your `MatrixMatch` folder.
2. In the bottom-right corner of the PyCharm window, you'll see text showing the current Python Interpreter (it might say "No interpreter"). Click it, then click **Add New Interpreter** → **Add Local Interpreter...**.
   *(Alternatively, go to **File → Settings → Project: MatrixMatch → Python Interpreter** and click "Add Interpreter".)*
3. On the left side of the window that pops up, select **Conda Environment**.
4. Important: Select the **Use existing environment** radio button.
5. Expand the dropdown list and select `matrixmatch`.
6. Click **OK**. PyCharm will take a moment to load and index your packages.

---

## Step 7: Run the App

1. Once PyCharm finishes setting up the environment, open `app.py`.
2. Right-click anywhere in the `app.py` code window and select **Run 'app'**.
3. Alternatively, straight from your Anaconda Prompt (making sure `(matrixmatch)` is active), you can type:
   ```bash
   python app.py
   ```
4. Look at the console at the bottom of the screen. When it says `Running on http://127.0.0.1:5000`, click the link to open MatrixMatch in your browser!

---

### Troubleshooting
- **`ModuleNotFoundError` in PyCharm:** PyCharm is likely trying to use your system's default Python. Re-read **Step 6** to make sure you successfully linked the `matrixmatch` Conda environment.
- **Can't connect to MySQL / Database:** Make sure XAMPP is actually open and running both Apache & MySQL. Check that your credentials in `.env` match your database exactly.
