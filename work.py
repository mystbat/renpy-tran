import requests
import configparser
import os
import json
import time

# 获取当前脚本的目录
current_directory = os.path.dirname(os.path.abspath(__file__))

# 创建保存响应的目录（如果不存在）
response_dir = os.path.join(current_directory, 'text')
os.makedirs(response_dir, exist_ok=True)

# 读取配置文件
config = configparser.ConfigParser()
config_path = os.path.join(current_directory, 'config.ini')

# 打印调试信息
print("开始读取配置文件...")
start_time = time.time()

# 检查配置文件是否存在
if not os.path.exists(config_path):
    # 如果配置文件不存在，创建一个默认配置
    config.add_section('API')
    config.set('API', 'url', 'https://api.siliconflow.cn/')
    config.set('API', 'api_key', '你的API Key')  # 请替换为你的实际API密钥

    config.add_section('Model')
    config.set('Model', 'model', 'deepseek-ai/DeepSeek-R1-0528-Qwen3-8B')
    config.set('Model', 'max_tokens', '8192')
    config.set('Model', 'min_p', '0.05')
    config.set('Model', 'temperature', '0.2')
    config.set('Model', 'top_p', '0.9')
    config.set('Model', 'top_k', '30')

    with open(config_path, 'w', encoding='utf-8') as configfile:
        config.write(configfile)
    print(f"已创建默认配置文件: {config_path}")
else:
    print(f"配置文件已存在: {config_path}")

# 读取配置文件
try:
    config.read(config_path)
    print(f"配置文件读取成功，耗时: {time.time() - start_time:.2f}秒")
except configparser.Error as e:
    print(f"配置文件解析错误: {str(e)}")
    exit()

# 从配置文件获取参数
api_url = config.get('API', 'url') + '/v1/chat/completions'
api_key = config.get('API', 'api_key')

model = config.get('Model', 'model')
max_tokens = config.getint('Model', 'max_tokens')
min_p = config.getfloat('Model', 'min_p')
temperature = config.getfloat('Model', 'temperature')
top_p = config.getfloat('Model', 'top_p')
top_k = config.getint('Model', 'top_k')

# 打印模型参数
print(f"模型参数加载成功:")
print(f"模型: {model}")
print(f"最大token数: {max_tokens}")
print(f"最小p值: {min_p}")
print(f"温度: {temperature}")
print(f"top_p: {top_p}")
print(f"top_k: {top_k}")

# 读取消息文件
try:
    print("正在读取消息文件...")
    start_time = time.time()
    # 读取消息文本文件
    with open(os.path.join(current_directory, 'message.txt'), 'r', encoding='utf-8') as message_file:
        user_message = message_file.read().strip()

    # 读取上下文文件
    context_file_path = os.path.join(current_directory, 'top_message.json')
    context_messages = []
    if os.path.exists(context_file_path):
        with open(context_file_path, 'r', encoding='utf-8') as context_file:
            context_messages = json.load(context_file)
            print(f"已加载上下文消息，共 {len(context_messages)} 条")

    # 检查第一行是否为注释
    lines = user_message.splitlines()
    comment_line = None
    if lines:
        first_line = lines[0].strip()
        if first_line.startswith('#') and '.rpy' in first_line:
            comment_line = first_line
            print(f"检测到注释行: {comment_line}")

    print(f"消息文件读取成功，耗时: {time.time() - start_time:.2f}秒")
except Exception as e:
    print(f"文件读取错误: {str(e)}")
    exit()

# 构建请求参数
print("正在构建请求参数...")
start_time = time.time()
payload = {
    "model": model,
    "messages": context_messages + [
        {
            "role": "user",
            "content": user_message
        }
    ],
    "stream": False,
    "thinking_budget": 4096,
    "min_p": min_p,
    "temperature": temperature,
    "top_p": top_p,
    "top_k": top_k,
    "max_tokens": max_tokens,
    "response_format": {"type": "text"},
}

headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json"
}

print(f"请求参数构建完成，耗时: {time.time() - start_time:.2f}秒")
print(f"请求URL: {api_url}")
print(f"请求参数大小: {len(json.dumps(payload))} 字节")

# 发送请求
print("正在发送请求到模型API...")
start_time = time.time()
response = requests.request("POST", api_url, json=payload, headers=headers)
print(f"请求发送完成，耗时: {time.time() - start_time:.2f}秒")

# 输出响应
print("收到模型响应，正在处理...")
start_time = time.time()

try:
    response_data = response.json()
    print(f"响应状态码: {response.status_code}")
    print(f"响应内容: {json.dumps(response_data, indent=2, ensure_ascii=False)}")

    # 检查响应是否成功 - 只检查HTTP状态码，不检查code字段
    if response.status_code == 200:
        # 获取模型反馈内容 - 直接从choices[0].message.content获取
        model_response = response_data['choices'][0]['message']['content']

        # 先保存到content.txt（覆盖方式）
        content_path = os.path.join(response_dir, 'content.txt')
        with open(content_path, 'w', encoding='utf-8') as f:
            f.write(model_response)
            f.write('\n')  # 添加换行符

        print(f"模型回答已保存到临时文件: {content_path}")

        # 然后继续原有的保存逻辑，保存到response.txt（追加方式）
        save_path = os.path.join(response_dir, 'response.txt')

        # 创建写入内容
        write_content = []
        if comment_line:
            write_content.append(comment_line)
        write_content.append(model_response)

        # 使用追加模式写入文件
        with open(save_path, 'a', encoding='utf-8') as f:
            for line in write_content:
                f.write(line + '\n')

        print(f"模型回答已保存到文件: {save_path}")
    else:
        # 如果响应不成功，不保存任何内容
        print(f"响应不成功，状态码: {response.status_code}")

    print(f"响应处理完成，耗时: {time.time() - start_time:.2f}秒")

except json.JSONDecodeError:
    print("响应解析错误: 响应内容不是有效的JSON格式")
    print(f"原始响应: {response.text}")
