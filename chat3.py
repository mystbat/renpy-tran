import configparser
import requests
import os
import json
import logging
import time

# 设置日志记录
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# 读取配置文件
config = configparser.ConfigParser()
try:
    config.read('config.ini')
except configparser.Error as e:
    logging.error(f"配置文件解析错误: {str(e)}")
    exit(1)

# 配置验证
try:
    api_key = config['API']['api_key']
    api_url = config['API']['url']
    model = config['Model']['model']
    max_tokens = int(config['Model']['max_tokens'])
    min_p = float(config['Model']['min_p'])
    temperature = float(config['Model']['temperature'])
    top_p = float(config['Model']['top_p'])
    top_k = int(config['Model']['top_k'])
except KeyError as e:
    logging.error(f"配置文件缺少必要字段: {e}")
    exit(1)

# 请求头
headers = {
    'Authorization': f'Bearer {api_key}',
    'Content-Type': 'application/json'
}

# 初始化对话历史
history_messages = []
if os.path.exists('top_message.json'):
    try:
        with open('top_message.json', 'r', encoding='utf-8') as f:
            history_messages = json.load(f)
    except json.JSONDecodeError:
        logging.warning("文件 top_message.json 格式错误，使用默认系统提示。")
        history_messages = [{"role": "system", "content": "You are a helpful assistant."}]
else:
    # 如果历史文件不存在，使用默认系统提示
    history_messages = [{"role": "system", "content": "You are a helpful assistant."}]

# 定义目录路径
split_files_dir = 'split_files'
text_dir = 'text'
solved_dir = 'solved'

# 确保目录存在
os.makedirs(split_files_dir, exist_ok=True)
os.makedirs(text_dir, exist_ok=True)
os.makedirs(solved_dir, exist_ok=True)


def send_to_model(messages, retries=3, backoff_factor=2, timeout=30):
    data = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "min_p": min_p,
        "temperature": temperature,
        "top_p": top_p,
        "top_k": top_k
    }

    for attempt in range(retries):
        try:
            response = requests.post(api_url, headers=headers, json=data, timeout=timeout)
            response.raise_for_status()
            result = response.json()
        except requests.exceptions.RequestException as e:
            logging.error(f"请求失败 (尝试 {attempt + 1}/{retries}): {str(e)}")
            if attempt < retries - 1:
                time.sleep(backoff_factor * (2 ** attempt))
            else:
                return None
        except requests.exceptions.JSONDecodeError:
            logging.error(f"无效的JSON响应: {response.text if response else '无响应内容'}")
            return None

        if response and response.status_code == 200:
            if 'choices' in result and len(result['choices']) > 0:
                reply_content = result['choices'][0]['message']['content']
                logging.info(f"模型回复: {reply_content}")
                return reply_content
            else:
                logging.warning(f"API返回无效数据: {result}")
                return None
        else:
            logging.warning("请求未成功，无法获取回复。")
            return None


def process_file(file_path):
    with open(file_path, 'r', encoding='utf-8') as file:
        user_input = file.read()

    messages = history_messages + [{"role": "user", "content": user_input}]
    reply_content = send_to_model(messages)

    if reply_content:
        base_name = os.path.basename(file_path).replace('.rpy', '')
        output_file = os.path.join(text_dir, f"{base_name}.txt")
        with open(output_file, 'w', encoding='utf-8') as out_file:
            out_file.write(reply_content)
        logging.info(f"反馈已保存到 {output_file}")


def batch_process():
    files = sorted([f for f in os.listdir(split_files_dir) if f.endswith('.rpy')])
    for i, file_name in enumerate(files):
        file_path = os.path.join(split_files_dir, file_name)

        # 检查文件是否已处理
        if file_name.endswith('.solved'):
            logging.info(f"文件 {file_name} 已处理，跳过...")
            continue

        process_file(file_path)

        # 移动已处理的文件到 solved 目录
        solved_path = os.path.join(solved_dir, file_name)
        shutil.move(file_path, solved_path)
        logging.info(f"文件 {file_name} 已移动到 {solved_dir}")


# 选择输入方式
print("请选择输入方式：")
print("1. 打字输入内容发送给聊天模型")
print("2. 选取message.txt的内容发送给聊天模型")
print("3. 批处理 split_files 目录中的文件")
choice = input("请输入选项 (1 或 2 或 3): ")

if choice not in ('1', '2', '3'):
    logging.error("无效的选项，请输入 1、2 或 3。")
    exit(1)

# 获取用户输入并处理
if choice == '1':
    while True:
        # 打字输入内容
        user_input = input("请输入内容 (输入 'exit' 退出): ")
        if user_input.lower() == 'exit':
            break
        messages = history_messages + [{"role": "user", "content": user_input}]
        reply_content = send_to_model(messages)
        if reply_content:
            print('回复:', reply_content)
elif choice == '2':
    try:
        with open('message.txt', 'r', encoding='utf-8') as file:
            user_input = file.read()
    except FileNotFoundError:
        logging.error("message.txt 文件未找到，请检查文件路径。")
        exit(1)
    messages = history_messages + [{"role": "user", "content": user_input}]
    reply_content = send_to_model(messages)
    if reply_content:
        print(f"模型回复: {reply_content}")
elif choice == '3':
    batch_process()


