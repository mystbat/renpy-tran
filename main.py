import os
import subprocess
import shutil


def main():
    print("欢迎使用游戏本地化工具")
    print("请选择操作:")
    print("1. 分割文件 (运行 split.py)")
    print("2. 编辑配置文件 (config.ini)")
    print("3. AI批处理")
    print("4. 退出")

    while True:
        choice = input("请输入选项 (1-4): ").strip()

        if choice == "1":
            # 运行 split.py
            split_script = os.path.join(os.path.dirname(__file__), "split.py")
            if os.path.exists(split_script):
                subprocess.run(["python", split_script])
            else:
                print("错误: split.py 文件不存在!")

        elif choice == "2":
            # 编辑 config.ini
            config_path = os.path.join(os.path.dirname(__file__), "config.ini")
            if os.path.exists(config_path):
                # 根据操作系统打开编辑器
                if os.name == "nt":  # Windows
                    os.system(f'notepad "{config_path}"')
                elif os.name == "posix":  # Unix/Linux/Mac
                    if os.uname().sysname == "Darwin":  # Mac
                        os.system(f'open "{config_path}"')
                    else:  # Linux
                        os.system(f'xdg-open "{config_path}"')
                else:
                    print("未知操作系统，无法打开编辑器")
            else:
                print("错误: config.ini 文件不存在!")

        elif choice == "3":
            # AI批处理
            batch_process()

        elif choice == "4":
            print("感谢使用，再见!")
            break

        else:
            print("无效选项，请重新输入!")


def batch_process():
    # 创建必要的目录
    script_dir = os.path.dirname(os.path.abspath(__file__))
    split_dir = os.path.join(script_dir, "split_files")
    solved_dir = os.path.join(script_dir, "solved")

    if not os.path.exists(split_dir):
        print(f"错误: {split_dir} 目录不存在!")
        return

    if not os.path.exists(solved_dir):
        os.makedirs(solved_dir)

    # 获取所有rpy文件 - 修改为查找所有以数字序号.rpy结尾的文件
    rpy_files = []
    for filename in os.listdir(split_dir):
        if filename.endswith('.rpy'):
            # 检查文件名是否包含数字序号
            if any(char.isdigit() for char in filename):
                rpy_files.append(filename)

    if not rpy_files:
        print("错误: split_files目录中没有找到符合命名规则的rpy文件!")
        print("符合规则的文件名应该包含数字序号，例如: script_exp_split_001.rpy")
        return

    print(f"找到 {len(rpy_files)} 个符合规则的文件进行处理")

    # 按照数字序号排序
    rpy_files.sort(key=lambda f: int(''.join(filter(str.isdigit, f))))

    for i, rpy_file in enumerate(rpy_files):
        rpy_path = os.path.join(split_dir, rpy_file)

        # 读取并提取最前的文件文本内容
        try:
            with open(rpy_path, 'r', encoding='utf-8') as f:
                file_content = f.read()

            # 将内容写入message.txt
            message_path = os.path.join(script_dir, "message.txt")
            with open(message_path, 'w', encoding='utf-8') as f:
                f.write(file_content)

            print(f"已处理文件 {i + 1}/{len(rpy_files)}: {rpy_file}")

            # 运行work.py
            work_script = os.path.join(script_dir, "work.py")
            if os.path.exists(work_script):
                subprocess.run(["python", work_script])
            else:
                print(f"警告: work.py 文件不存在，跳过处理")
                continue

            # 移动文件到solved目录
            solved_path = os.path.join(solved_dir, rpy_file)
            shutil.move(rpy_path, solved_path)
            print(f"文件已移动到solved目录: {rpy_file}")

        except Exception as e:
            print(f"处理文件 {rpy_file} 时出错: {str(e)}")

    print("所有文件处理完成!")


if __name__ == "__main__":
    main()
