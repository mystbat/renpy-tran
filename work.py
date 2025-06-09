import configparser
import requests
import os

# 读取配置文件
config = configparser.ConfigParser()
config.read('config.ini')

# 获取API配置
api_key = config['API']['api_key']
api_url = config['API']['url']

# 获取模型配置
model = config['Model']['model']
max_tokens = int(config['Model']['max_tokens'])
min_p = float(config['Model']['min_p'])
temperature = float(config['Model']['temperature'])
top_p = float(config['Model']['top_p'])
top_k = int(config['Model']['top_k'])

# 请求头
headers = {
    'Authorization': f'Bearer {api_key}',
    'Content-Type': 'application/json'
}

# 选择输入方式
print("请选择输入方式：")
print("1. 打字输入内容发送给聊天模型")
print("3. 从pending文件夹中按顺序发送文件给聊天模型")
choice = input("请输入选项 (1 或 3): ")

if choice == '1':
    while True:
        # 打字输入内容
        user_input = input("请输入内容 (输入 'exit' 退出): ")
        if user_input.lower() == 'exit':
            break
        messages = [
            {'role': 'system', 'content': 'You are a helpful assistant.'},
            {'role': 'user', 'content': user_input}
        ]

        # 请求体
        data = {
            'model': model,
            'messages': messages,
            'max_tokens': max_tokens,
            'min_p': min_p,
            'temperature': temperature,
            'top_p': top_p,
            'top_k': top_k
        }

        # 发送POST请求
        response = requests.post(api_url, headers=headers, json=data)

        # 检查响应状态码
        if response.status_code == 200:
            # 解析响应
            result = response.json()
            reply_content = result['choices'][0]['message']['content']

            # 打印回复内容到屏幕上
            print('回复:', reply_content)
        else:
            print('请求失败:', response.status_code, response.text)
elif choice == '3':
    # 从pending文件夹中按顺序发送文件给聊天模型
    pending_folder = 'pending'
    solved_folder = 'solved'
    text_folder = 'text'

    if not os.path.exists(pending_folder):
        print("pending 文件夹未找到，请检查文件路径。")
        exit(1)

    if not os.path.exists(solved_folder):
        os.makedirs(solved_folder)

    if not os.path.exists(text_folder):
        os.makedirs(text_folder)

    # 获取pending文件夹中的所有文件并按字母顺序排序
    files = sorted([f for f in os.listdir(pending_folder) if os.path.isfile(os.path.join(pending_folder, f))])

    for file_name in files:
        file_path = os.path.join(pending_folder, file_name)

        # 检查文件是否已处理
        if file_name.endswith('.solved'):
            print(f"文件 {file_name} 已处理，跳过...")
            continue

        with open(file_path, 'r', encoding='utf-8') as file:
            user_input = file.read()
            messages = [
                {'role': 'system', 'content': 'You are a helpful assistant.'},
                {'role': 'user', 'content': user_input}
            ]

            # 请求体
            data = {
                'model': model,
                'messages': messages,
                'max_tokens': max_tokens,
                'min_p': min_p,
                'temperature': temperature,
                'top_p': top_p,
                'top_k': top_k
            }

            # 发送POST请求
            response = requests.post(api_url, headers=headers, json=data)

            # 检查响应状态码
            if response.status_code == 200:
                # 解析响应
                result = response.json()
                reply_content = result['choices'][0]['message']['content']

                # 打印回复内容到屏幕上
                print('回复:', reply_content)
            else:
                print('请求失败:', response.status_code, response.text)

        # 标记文件为已处理
        os.rename(file_path, f"{file_path}.solved")
else:
    print("无效的选项，请输入 1 或 3。")
    exit(1)