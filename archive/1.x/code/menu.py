#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
CSV文件处理工具主菜单
提供多种CSV文件处理功能的一站式解决方案
"""

import os
import subprocess
import sys


def clear_screen():
    """清屏函数，不同操作系统使用不同清屏命令"""
    os.system('cls' if os.name == 'nt' else 'clear')


def print_menu():
    """打印主菜单"""
    clear_screen()
    print("=" * 50)
    print("CSV文件处理工具主菜单")
    print("=" * 50)
    print("1. 转换为CSV格式")
    print("2. 分割CSV文件")
    print("3. 运行翻译功能")
    print("4. 检查错误")
    print("5. 合并输出文件")
    print("6. 转换为RPY格式")
    print("7. 退出程序")
    print("=" * 50)


def execute_command(command):
    """执行命令并捕获输出"""
    try:
        result = subprocess.run(command,
                                shell=True,
                                check=True,
                                text=True,
                                capture_output=True,
                                encoding='utf-8')
        return result.stdout
    except subprocess.CalledProcessError as e:
        return f"执行错误: {e.stderr}"
    except Exception as e:
        return f"发生异常: {str(e)}"


def option1_convert_to_csv():
    """转换为CSV格式"""
    clear_screen()
    print("正在执行: 转换为CSV格式")
    print("-" * 50)

    # 获取当前目录下的所有.rpy文件
    rpy_files = [f for f in os.listdir('.')
                 if f.endswith('.rpy') and not f.startswith('.')]  # 忽略隐藏文件

    if not rpy_files:
        print("错误: 当前目录下没有找到.rpy文件！")
        input("按Enter键返回主菜单...")
        return

    print("找到以下.rpy文件:")
    for i, file in enumerate(rpy_files, 1):
        print(f"{i}. {file}")

    try:
        choice = int(input("\n请选择要转换的.rpy文件编号 [1-{}]:".format(len(rpy_files))))
        if choice < 1 or choice > len(rpy_files):
            raise ValueError("无效的选择")

        selected_file = rpy_files[choice - 1]
        print(f"\n正在转换: {selected_file} -> {os.path.splitext(selected_file)[0]}.csv")

        # 执行转换命令
        result = execute_command(f"python tocsv.py {selected_file}")
        print(result)

        input("\n转换完成! 按Enter键返回主菜单...")

    except ValueError as e:
        print(f"错误: {str(e)}")
        input("按Enter键返回主菜单...")
    except Exception as e:
        print(f"发生错误: {str(e)}")
        input("按Enter键返回主菜单...")


def option2_split_csv():
    """分割CSV文件"""
    clear_screen()
    print("正在执行: 分割CSV文件")
    print("-" * 50)

    # 获取当前目录下的所有.csv文件
    csv_files = [f for f in os.listdir('.')
                 if f.endswith('.csv') and not f.startswith('.')]  # 忽略隐藏文件

    if not csv_files:
        print("错误: 当前目录下没有找到.csv文件！")
        input("按Enter键返回主菜单...")
        return

    print("找到以下CSV文件:")
    for i, file in enumerate(csv_files, 1):
        print(f"{i}. {file}")

    try:
        choice = int(input("\n请选择要分割的CSV文件编号 [1-{}]:".format(len(csv_files))))
        if choice < 1 or choice > len(csv_files):
            raise ValueError("无效的选择")

        selected_file = csv_files[choice - 1]
        print(f"\n正在分割: {selected_file}")

        # 执行分割命令
        result = execute_command(f"python split_csv.py {selected_file}")
        print(result)

        input("\n分割完成! 按Enter键返回主菜单...")

    except ValueError as e:
        print(f"错误: {str(e)}")
        input("按Enter键返回主菜单...")
    except Exception as e:
        print(f"发生错误: {str(e)}")
        input("按Enter键返回主菜单...")


def option3_run_translation():
    """运行翻译功能"""
    clear_screen()
    print("正在执行: 运行翻译功能")
    print("-" * 50)

    # 执行翻译命令（在新窗口）
    print("翻译功能将在新窗口运行...")
    subprocess.Popen(f'python fix_csv.py', shell=True)
    # 等待用户手动关闭新窗口
    input("\n新窗口已打开，请手动关闭并按Enter键返回主菜单...")


def option4_check_errors():
    """检查错误"""
    clear_screen()
    print("正在执行: 检查错误")
    print("-" * 50)

    # 获取当前目录下的所有.csv文件
    csv_files = [f for f in os.listdir('.')
                 if f.endswith('.csv') and not f.startswith('.')]  # 忽略隐藏文件

    if not csv_files:
        print("错误: 当前目录下没有找到.csv文件！")
        input("按Enter键返回主菜单...")
        return

    print("找到以下CSV文件:")
    for i, file in enumerate(csv_files, 1):
        print(f"{i}. {file}")

    try:
        choice = int(input("\n请选择要检查的CSV文件编号 [1-{}]:".format(len(csv_files))))
        if choice < 1 or choice > len(csv_files):
            raise ValueError("无效的选择")

        selected_file = csv_files[choice - 1]
        print(f"\n正在检查: {selected_file}")

        # 执行检查命令
        result = execute_command(f"python check-csv.py {selected_file}")
        print(result)

        input("\n检查完成! 按Enter键返回主菜单...")

    except ValueError as e:
        print(f"错误: {str(e)}")
        input("按Enter键返回主菜单...")
    except Exception as e:
        print(f"发生错误: {str(e)}")
        input("按Enter键返回主菜单...")


def option5_merge_output():
    """合并输出文件"""
    clear_screen()
    print("正在执行: 合并输出文件")
    print("-" * 50)

    # 获取当前目录下的所有.csv文件
    csv_files = [f for f in os.listdir('.')
                 if f.endswith('.csv') and not f.startswith('.')]  # 忽略隐藏文件

    if not csv_files:
        print("错误: 当前目录下没有找到.csv文件！")
        input("按Enter键返回主菜单...")
        return

    print("找到以下CSV文件:")
    for i, file in enumerate(csv_files, 1):
        print(f"{i}. {file}")

    try:
        choice = int(input("\n请选择要合并的CSV文件编号 [1-{}]:".format(len(csv_files))))
        if choice < 1 or choice > len(csv_files):
            raise ValueError("无效的选择")

        selected_file = csv_files[choice - 1]
        print(f"\n正在合并: {selected_file}")

        # 执行合并命令
        result = execute_command(f"python output.py {selected_file}")
        print(result)

        input("\n合并完成! 按Enter键返回主菜单...")

    except ValueError as e:
        print(f"错误: {str(e)}")
        input("按Enter键返回主菜单...")
    except Exception as e:
        print(f"发生错误: {str(e)}")
        input("按Enter键返回主菜单...")


def option6_convert_to_rpy():
    """转换为RPY格式"""
    clear_screen()
    print("正在执行: 转换为RPY格式")
    print("-" * 50)

    # 获取当前目录下的所有.csv文件
    csv_files = [f for f in os.listdir('.')
                 if f.endswith('.csv') and not f.startswith('.')]  # 忽略隐藏文件

    if not csv_files:
        print("错误: 当前目录下没有找到.csv文件！")
        input("按Enter键返回主菜单...")
        return

    print("找到以下CSV文件:")
    for i, file in enumerate(csv_files, 1):
        print(f"{i}. {file}")

    try:
        choice = int(input("\n请选择要转换的CSV文件编号 [1-{}]:".format(len(csv_files))))
        if choice < 1 or choice > len(csv_files):
            raise ValueError("无效的选择")

        selected_file = csv_files[choice - 1]
        print(f"\n正在转换: {selected_file} -> {os.path.splitext(selected_file)[0]}.rpy")

        # 执行转换命令
        result = execute_command(f"python csvtorpy.py {selected_file}")
        print(result)

        input("\n转换完成! 按Enter键返回主菜单...")

    except ValueError as e:
        print(f"错误: {str(e)}")
        input("按Enter键返回主菜单...")
    except Exception as e:
        print(f"发生错误: {str(e)}")
        input("按Enter键返回主菜单...")


def main():
    """主函数"""
    while True:
        print_menu()

        try:
            choice = int(input("请输入选项编号 [1-7]: "))

            if choice == 1:
                option1_convert_to_csv()
            elif choice == 2:
                option2_split_csv()
            elif choice == 3:
                option3_run_translation()
            elif choice == 4:
                option4_check_errors()
            elif choice == 5:
                option5_merge_output()
            elif choice == 6:
                option6_convert_to_rpy()
            elif choice == 7:
                print("感谢使用CSV文件处理工具，再见！")
                break
            else:
                print("无效的选择，请重新输入！")
                input("按Enter键返回主菜单...")

        except ValueError:
            print("错误: 请输入有效的数字选项！")
            input("按Enter键返回主菜单...")
        except KeyboardInterrupt:
            print("\n程序被用户中断，正在返回主菜单...")
            input("按Enter键继续...")
        except Exception as e:
            print(f"发生未知错误: {str(e)}")
            input("按Enter键返回主菜单...")


if __name__ == "__main__":
    main()
