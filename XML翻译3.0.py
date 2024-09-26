import os
import shutil
import sqlite3
from openai import OpenAI
import xml.etree.ElementTree as ET
import re
import logging
import toml
from concurrent.futures import ThreadPoolExecutor, as_completed
from xml.dom import minidom

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# 定义翻译存储库数据库的路径和配置文件路径
content_db_path = '翻译存储库.db'
config_file_path = 'config.toml'

# 创建示例配置文件
def create_config_file():
    config_content = """
# 配置文件示例
# OpenAI API 密钥
# 参考示例填写，所有兼容openai API接口的都可以使用。
open_apikey = "YOUR_API_KEY"

# OpenAI API 基础 URL
# 参考示例填写，所有兼容openai API接口的都可以使用。
open_base_url = "https://api.deepseek.com"

# 最大并发线程数
# 请不要设置过大，具体参数请根据你使用的ai接口的限速TPS|TPM进行调整，通常情况下一个短句的翻译用时在0.3秒到1秒左右。
max_workers = 5

# 输入目录
# 脚本会从此目录获取需要翻译的语言文件。
input_dir = "input"

# 解包的MOD文件夹根目录
# 会自动遍历所在目录的所有子目录寻找语言文件。
user_root_dir = "YOUR_ROOT_DIR"

# 输出的 XML 文件名
# 脚本翻译后的文件输出名称。
output_xml_file = "translated_content.xml"

# 需要手动调整翻译内容的可以通过任意工具打开Sqlite数据库【翻译存储库.db】，对翻译内容进行调整后再次运行脚本即可。
    """
    with open(config_file_path, 'w', encoding='utf-8') as config_file:
        config_file.write(config_content.strip())
    logging.info(f"配置文件 '{config_file_path}' 已创建。请更新相应的参数。")

# 读取配置文件
def load_config():
    try:
        with open(config_file_path, 'r', encoding='utf-8') as f:
            config = toml.load(f)
        return config
    except FileNotFoundError:
        logging.warning("配置文件未找到，正在创建示例配置文件...")
        create_config_file()
        logging.info("示例配置文件已创建，请根据需要进行编辑。")

        # 等待用户确认
        input("按 Enter 键退出...")  # 等待用户输入
        return None

# 创建输入目录
def create_input_directory(input_dir):
    if not os.path.exists(input_dir):
        os.makedirs(input_dir)
        logging.info(f"输入目录 '{input_dir}' 已创建。")

# 创建翻译存储库数据库和表
def create_content_database():
    conn = sqlite3.connect(content_db_path)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS terms (
            contentuid TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            translated_content TEXT
        )
    ''')
    conn.commit()
    conn.close()

# 验证 contentuid 的有效性
def is_valid_contentuid(contentuid):
    return bool(re.match(r'^[a-zA-Z0-9]{37}$', contentuid))

# 检查 contentuid 是否已存在于数据库中
def contentuid_exists(contentuid):
    conn = sqlite3.connect(content_db_path)
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM terms WHERE contentuid = ?', (contentuid,))
    exists = cursor.fetchone()[0] > 0
    conn.close()
    return exists

# 移动 English 文件夹中的 XML 文件到输入目录
def move_english_xml_files(user_root_dir, input_dir):
    localization_path = os.path.join(user_root_dir, 'Localization')
    if not os.path.exists(localization_path):
        logging.warning(f"未找到 Localization 文件夹: {localization_path}")
        return

    for root, dirs, files in os.walk(localization_path):
        if 'English' in dirs:
            english_folder = os.path.join(root, 'English')
            for file in os.listdir(english_folder):
                if file.endswith('.xml'):
                    src_file_path = os.path.join(english_folder, file)
                    dest_file_path = os.path.join(input_dir, file)
                    shutil.move(src_file_path, dest_file_path)
                    logging.info(f"移动文件: {src_file_path} 到 {dest_file_path}")

# 从 Chinese 文件夹中提取内容并存储到数据库
def extract_content_from_chinese(user_root_dir):
    localization_path = os.path.join(user_root_dir, 'Localization')
    chinese_folder = os.path.join(localization_path, 'Chinese')

    if not os.path.exists(chinese_folder):
        logging.warning(f"未找到 Chinese 文件夹: {chinese_folder}")
        return

    conn = sqlite3.connect(content_db_path)
    cursor = conn.cursor()

    for root, dirs, files in os.walk(chinese_folder):
        for file in files:
            if file.endswith('.xml'):
                file_path = os.path.join(root, file)
                try:
                    tree = ET.parse(file_path)
                    root_element = tree.getroot()
                    for content in root_element.findall('.//content'):
                        contentuid = content.get('contentuid')
                        text_content = content.text.strip() if content.text else ""
                        if contentuid and text_content and is_valid_contentuid(contentuid) and not contentuid_exists(contentuid):
                            cursor.execute('INSERT INTO terms (contentuid, translated_content) VALUES (?, ?)', (contentuid, text_content))
                            logging.info(f"Inserted: contentuid={contentuid}, translated_content={text_content}")
                        else:
                            logging.warning(f"Skipping: Invalid contentuid or already exists in {file_path}")
                except ET.ParseError as e:
                    logging.error(f"Parsing error: {e} - File: {file_path}")

    conn.commit()
    conn.close()

# 使用 OpenAI API 进行翻译
def ai_translate(content, open_apikey, open_base_url):
    client = OpenAI(api_key=open_apikey, base_url=open_base_url)

    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": f"请将以下内容翻译成中文：{content}"}]
        )
        return response.choices[0].message.content
    except Exception as e:
        logging.error(f"翻译失败: {e}")
        return None

# 提交内容进行翻译并存储结果
def translate_and_store(open_apikey, open_base_url, max_workers):
    conn = sqlite3.connect(content_db_path)
    cursor = conn.cursor()

    cursor.execute('SELECT contentuid, translated_content FROM terms WHERE translated_content IS NULL')
    content_items = cursor.fetchall()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_content = {executor.submit(ai_translate, content, open_apikey, open_base_url): contentuid for contentuid, content in content_items}

        for future in as_completed(future_to_content):
            contentuid = future_to_content[future]
            try:
                translated_content = future.result()
                if translated_content:
                    logging.info(f"Translated: contentuid={contentuid}, translated={translated_content}")
                    cursor.execute('UPDATE terms SET translated_content = ? WHERE contentuid = ?', (translated_content, contentuid))
            except Exception as e:
                logging.error(f"Error translating contentuid={contentuid}: {e}")

    conn.commit()
    conn.close()

# 美化 XML 文件
def pretty_xml(element):
    xml_str = ET.tostring(element, encoding='utf-8', xml_declaration=True)
    parsed_xml = minidom.parseString(xml_str)
    return parsed_xml.toprettyxml(indent="  ")

# 创建 XML 文件
def create_xml_file(output_file):
    conn = sqlite3.connect(content_db_path)
    cursor = conn.cursor()

    cursor.execute('SELECT contentuid, translated_content FROM terms WHERE translated_content IS NOT NULL')
    content_items = cursor.fetchall()

    root = ET.Element("contentList")
    for contentuid, translated_content in content_items:
        content_element = ET.SubElement(root, "content", contentuid=contentuid, version="1")
        content_element.text = translated_content

    pretty_xml_str = pretty_xml(root)
    with open(output_file, 'w', encoding='utf-8') as xml_file:
        xml_file.write(pretty_xml_str)
    logging.info(f"XML 文件 '{output_file}' 已创建。")

# 主函数
def main():
    # 读取配置
    config = load_config()
    if config is None:  # 如果配置加载失败，直接返回
        return

    open_apikey = config.get('open_apikey')
    open_base_url = config.get('open_base_url')
    max_workers = config.get('max_workers', 5)
    input_dir = config.get('input_dir', 'input')
    user_root_dir = config.get('user_root_dir', 'YOUR_ROOT_DIR')
    output_xml_file = config.get('output_xml_file', 'translated_content.xml')

    # 创建输入目录
    create_input_directory(input_dir)

    create_content_database()
    logging.info(f"内容数据库 '{content_db_path}' 已创建。")

    move_english_xml_files(user_root_dir, input_dir)
    extract_content_from_chinese(user_root_dir)

    logging.info("从 XML 文件中提取的数据已插入到翻译存储库。")

    translate_and_store(open_apikey, open_base_url, max_workers)
    logging.info("翻译结果已存储。")

    # 创建 XML 文件
    create_xml_file(output_xml_file)

if __name__ == '__main__':
    main()
