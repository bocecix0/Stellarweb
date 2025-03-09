import os
from dotenv import load_dotenv
import json
import re
import time
from flask import Flask, request, render_template_string, redirect, url_for
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
import anthropic
from cryptography.fernet import Fernet

# .env dosyasını yükle
load_dotenv()

# Anthropic API anahtarını çevre değişkeninden al
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# Şifreleme anahtarı
key = Fernet.generate_key()
cipher = Fernet(key)

# Dosya yolları
MACRO_FILE = "macros.json"
SESSION_FILE = "sessions.enc"

app = Flask(__name__)
driver = None

def create_webdriver():
    """Chrome WebDriver'ı oluşturur."""
    from selenium.webdriver.chrome.options import Options
    chrome_options = Options()
    chrome_options.add_argument("--start-maximized")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-gpu")
    driver = webdriver.Chrome(options=chrome_options)
    return driver

def highlight_elements(driver):
    """Sayfadaki öğeleri vurgular."""
    highlight_script = """
    function highlightElements() {
        document.querySelectorAll('.element-highlight').forEach(el => el.remove());
        const colors = {
            'input': 'rgba(52, 152, 219, 0.7)',
            'button': 'rgba(46, 204, 113, 0.7)',
            'a': 'rgba(231, 76, 60, 0.7)',
            'div': 'rgba(155, 89, 182, 0.7)',
            '[role="button"]': 'rgba(241, 196, 15, 0.7)'
        };
        function createHighlight(element, color) {
            const rect = element.getBoundingClientRect();
            const highlightDiv = document.createElement('div');
            highlightDiv.className = 'element-highlight';
            highlightDiv.style.position = 'absolute';
            highlightDiv.style.left = `${rect.left + window.scrollX}px`;
            highlightDiv.style.top = `${rect.top + window.scrollY}px`;
            highlightDiv.style.width = `${rect.width}px`;
            highlightDiv.style.height = `${rect.height}px`;
            highlightDiv.style.backgroundColor = 'transparent';
            highlightDiv.style.border = `3px solid ${color}`;
            highlightDiv.style.boxShadow = `0 0 5px ${color}`;
            highlightDiv.style.pointerEvents = 'none';
            highlightDiv.style.zIndex = '10000';
            highlightDiv.style.boxSizing = 'border-box';
            const labelDiv = document.createElement('div');
            labelDiv.style.position = 'absolute';
            labelDiv.style.top = '-20px';
            labelDiv.style.left = '0';
            labelDiv.style.backgroundColor = color;
            labelDiv.style.color = 'white';
            labelDiv.style.padding = '2px 5px';
            labelDiv.style.fontSize = '10px';
            labelDiv.textContent = `${element.tagName}: ${element.innerText.substring(0, 20)}`;
            highlightDiv.appendChild(labelDiv);
            document.body.appendChild(highlightDiv);
        }
        Object.keys(colors).forEach(selector => {
            const elements = document.querySelectorAll(selector);
            elements.forEach(el => {
                if (el.offsetWidth > 0 && el.offsetHeight > 0 && 
                    window.getComputedStyle(el).display !== 'none') {
                    createHighlight(el, colors[selector]);
                }
            });
        });
    }
    highlightElements();
    """
    driver.execute_script(highlight_script)

def parse_claude_response(response_text):
    """Claude'un yanıtını JSON formatına çevirir."""
    json_patterns = [r'```json\s*(.*?)\s*```', r'\{.*\}']
    for pattern in json_patterns:
        json_match = re.search(pattern, response_text, re.DOTALL | re.MULTILINE)
        if json_match:
            json_str = json_match.group(1) if len(json_match.groups()) > 0 else json_match.group(0)
            json_str = json_str.replace('\n', ' ').strip()
            try:
                return json.loads(json_str)
            except json.JSONDecodeError as e:
                print(f"JSON decode hatası: {e}\nProblematik JSON: {json_str}")
    print("JSON formatı bulunamadı")
    return {"actions": [], "explanation": "Yanıt işlenemedi"}

def scan_page_elements(driver):
    """Sayfadaki öğeleri tarar ve bilgileri döndürür."""
    print("Sayfa öğeleri taranıyor...")
    highlight_elements(driver)
    elements_info = {"input_elements": [], "button_elements": [], "link_elements": [], "interactive_elements": []}
    try:
        for inp in driver.find_elements(By.TAG_NAME, "input")[:20]:
            elements_info["input_elements"].append({
                "id": inp.get_attribute("id"),
                "name": inp.get_attribute("name"),
                "type": inp.get_attribute("type"),
                "placeholder": inp.get_attribute("placeholder")
            })
        buttons = driver.find_elements(By.TAG_NAME, "button")
        buttons.extend(driver.find_elements(By.CSS_SELECTOR, "[role='button']"))
        for btn in buttons[:20]:
            elements_info["button_elements"].append({
                "id": btn.get_attribute("id"),
                "text": btn.text,
                "aria-label": btn.get_attribute("aria-label")
            })
        for link in driver.find_elements(By.TAG_NAME, "a")[:20]:
            elements_info["link_elements"].append({
                "href": link.get_attribute("href"),
                "text": link.text[:30],
                "aria-label": link.get_attribute("aria-label")
            })
        return elements_info
    except Exception as e:
        print(f"Sayfa tarama hatası: {e}")
        return elements_info

def ask_claude_for_web_actions(client, user_command, page_elements=None):
    """Claude'dan web eylemleri için JSON yanıtı alır."""
    elements_json = json.dumps(page_elements) if page_elements else "{}"
    prompt = f"""
    Sen bir web otomasyon asistanısın. Aşağıdaki kullanıcı komutunu ve sayfa öğelerini analiz et,
    web tarayıcıda gerçekleştirilebilecek adımları JSON formatında döndür.
    
    Kullanıcı komutu: "{user_command}"
    Sayfa öğeleri: {elements_json}
    
    Yanıtını şu JSON formatında ver:
    {{
        "actions": [
            {{"type": "navigateTo", "url": "tam_url"}},
            {{"type": "click", "selector": "css_selector", "description": "ne tıkladığı"}},
            {{"type": "type", "selector": "css_selector", "text": "yazılacak_metin"}},
            {{"type": "pressKey", "key": "Enter"}},
            {{"type": "wait", "seconds": saniye_sayısı}}
        ],
        "explanation": "Bu eylemleri seçmemin nedeni..."
    }}
    """
    message = client.messages.create(
        model="claude-3-7-sonnet-20250219",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )
    return parse_claude_response(message.content[0].text)

def execute_web_actions(driver, actions_data, current_url):
    """Claude'dan gelen eylemleri Selenium ile çalıştırır."""
    for action in actions_data.get("actions", []):
        action_type = action.get("type", "")
        try:
            if action_type == "navigateTo":
                driver.get(action.get("url", ""))
                time.sleep(2)
                highlight_elements(driver)
                current_url = driver.current_url
            elif action_type == "click":
                element = WebDriverWait(driver, 15).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, action.get("selector", "")))
                )
                driver.execute_script("arguments[0].scrollIntoView(true);", element)
                element.click()
                time.sleep(1)
                highlight_elements(driver)
            elif action_type == "type":
                element = WebDriverWait(driver, 15).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, action.get("selector", "")))
                )
                element.clear()
                element.send_keys(action.get("text", ""))
            elif action_type == "pressKey":
                driver.switch_to.active_element.send_keys(Keys.ENTER)
                time.sleep(1)
                highlight_elements(driver)
            elif action_type == "wait":
                time.sleep(action.get("seconds", 1))
        except Exception as e:
            print(f"Eylem başarısız: {action_type} - {str(e)}")
    return current_url

# Flask Rotları
@app.route('/')
def index():
    """Ana sayfa: Web arayüzünü gösterir."""
    global driver
    if driver is None:
        driver = create_webdriver()
        driver.get("https://www.google.com")
        time.sleep(2)
        highlight_elements(driver)
    return render_template_string('''
        <html>
        <head>
            <title>Web Otomasyon Asistanı</title>
            <style>
                body { font-family: Arial, sans-serif; margin: 20px; }
                h1, h2 { color: #333; }
                form { margin-bottom: 20px; }
                label { margin-right: 10px; }
                input[type="text"] { padding: 5px; }
                input[type="submit"] { padding: 5px 10px; background-color: #4CAF50; color: white; border: none; cursor: pointer; }
                input[type="submit"]:hover { background-color: #45a049; }
            </style>
        </head>
        <body>
            <h1>Web Otomasyon Asistanı</h1>
            <form action="/run_command" method="POST">
                <label for="command">Komut:</label>
                <input type="text" id="command" name="command" size="50" placeholder="Örn: Google'da 'Python' ara">
                <input type="submit" value="Çalıştır">
            </form>
            <h2>Makro Kaydet</h2>
            <form action="/save_macro" method="POST">
                <label for="macro_name">Makro Adı:</label>
                <input type="text" id="macro_name" name="macro_name" placeholder="Örn: arama_macro">
                <label for="macro_command">Komut:</label>
                <input type="text" id="macro_command" name="macro_command" size="50" placeholder="Örn: Google'da 'Flask' ara">
                <input type="submit" value="Kaydet">
            </form>
            <h2>Makro Yükle</h2>
            <form action="/load_macro" method="POST">
                <label for="load_macro_name">Makro Adı:</label>
                <input type="text" id="load_macro_name" name="load_macro_name" placeholder="Örn: arama_macro">
                <input type="submit" value="Yükle">
            </form>
        </body>
        </html>
    ''')

@app.route('/run_command', methods=['POST'])
def run_command():
    """Kullanıcı komutunu çalıştırır."""
    command = request.form['command']
    page_elements = scan_page_elements(driver)
    actions_data = ask_claude_for_web_actions(client, command, page_elements)
    execute_web_actions(driver, actions_data, driver.current_url)
    return redirect(url_for('index'))

@app.route('/save_macro', methods=['POST'])
def save_macro():
    """Makroyu kaydeder."""
    name = request.form['macro_name']
    command = request.form['macro_command']
    actions_data = ask_claude_for_web_actions(client, command, scan_page_elements(driver))
    macros = {}
    if os.path.exists(MACRO_FILE):
        with open(MACRO_FILE, "r") as f:
            macros = json.load(f)
    macros[name] = actions_data
    with open(MACRO_FILE, "w") as f:
        json.dump(macros, f, indent=4)
    return redirect(url_for('index'))

@app.route('/load_macro', methods=['POST'])
def load_macro():
    """Kaydedilmiş makroyu yükler ve çalıştırır."""
    name = request.form['load_macro_name']
    if os.path.exists(MACRO_FILE):
        with open(MACRO_FILE, "r") as f:
            macros = json.load(f)
            actions_data = macros.get(name)
            if actions_data:
                execute_web_actions(driver, actions_data, driver.current_url)
    return redirect(url_for('index'))

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
