import os
import json
import requests
from configparser import ConfigParser

# 读取配置文件
config = ConfigParser()
config.read('config.ini')
api_url = config['API']['url']
headers = json.loads(config['API']['headers'])
payload = json.loads(config['API']['payload'])

# 定义文件夹路径
split_files_folder = 'split_files'

# 遍历文件夹中的文件
for filename in os.listdir(split_files_folder):
    if filename.endswith('.rpy'):
        file_path = os.path.join(split_files_folder, filename)
        with open(file_path, 'r') as file:
            file_content = file.read()

        # 首先发送一个问候消息
        initial_payload = {
            **payload,
            "messages": [
                {
                    "role": "user",
                    "content": "你好，请问可以开始处理文件了吗？"
                }
            ]
        }
        initial_response = requests.post(api_url, headers=headers, json=initial_payload)
        if initial_response.status_code == 200:
            initial_result = initial_response.json()
            print(f"Initial response: {initial_result}")
        else:
            print(f"Failed to get initial response: {initial_response.status_code} - {initial_response.text}")
            continue

        # 检查AI是否准备好
        if "ready" not in initial_result["choices"][0]["message"]["content"].lower():
            print("AI is not ready. Skipping file.")
            continue

        # 构建请求数据
        data = {
            **payload,
            "messages": [
                {
                    "role": "user",
                    "content": file_content
                }
            ]
        }

        # 发送请求
        response = requests.post(api_url, headers=headers, json=data)

        # 处理响应
        if response.status_code == 200:
            result = response.json()
            print(f"Processed {filename}: {result}")
        else:
            print(f"Failed to process {filename}: {response.status_code} - {response.text}")
