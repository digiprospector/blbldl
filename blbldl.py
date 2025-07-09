import argparse
import json
import logging
import os
import re
import requests
import sys
import time
from bs4 import BeautifulSoup
from fake_useragent import UserAgent
from lxml import etree
from pathlib import Path
from tqdm import tqdm
from typing import Dict, List, Optional, Tuple, Any, Union
from urllib.parse import unquote, urljoin
from user_agents import parse

# 配置日志记录
logger = logging.getLogger("blbldl")
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)

def get_sec_ch_ua_mobile(ua: str) -> str:
    """根据用户代理字符串确定是否为移动设备
    
    Args:
        ua: 用户代理字符串
        
    Returns:
        "?1" 表示移动设备，"?0" 表示非移动设备
    """
    user_agent = parse(ua)
    if user_agent.is_mobile:
        return "?1"
    else:
        return "?0"

def get_sec_ch_ua(user_agent: str) -> str:
    """从用户代理字符串中提取 Chrome 版本信息
    
    Args:
        user_agent: 用户代理字符串
        
    Returns:
        格式化的 sec-ch-ua 头部值
    """
    match = re.search(r"Chrome/(\d+)", user_agent)
    if match:
        version = match.group(1)
        return f'"Chromium";v="{version}", "Google Chrome";v="{version}", "Not.A/Brand";v="99"'
    return ""
    
def get_platform(user_agent: str) -> str:
    """从用户代理字符串中确定操作系统平台
    
    Args:
        user_agent: 用户代理字符串
        
    Returns:
        平台名称（iOS, macOS, Android, Windows 或 Unknown）
    """
    if "iPhone" in user_agent or "iPad" in user_agent:
        return "iOS"
    elif "Mac OS X" in user_agent and "Mobile" not in user_agent:
        return "macOS"
    elif "Android" in user_agent:
        return "Android"
    elif "Windows" in user_agent:
        return "Windows"
    else:
        return "Unknown"

def parse_bv_info(text: str) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """从B站视频页面HTML中解析视频信息
    
    从页面中提取两个关键的JSON数据：
    1. window.__playinfo__ - 包含媒体文件信息
    2. window.__INITIAL_STATE__ - 包含视频元数据
    
    Args:
        text: 视频页面的HTML内容
        
    Returns:
        包含两个元素的元组：(playinfo_json, initial_state_json)
        如果未找到相应数据，对应位置为None
    """
    data = None  # playinfo
    data1 = None  # initial state
    
    try:
        tree = etree.HTML(text)
        title = "".join(tree.xpath('.//div[@class="video-info-title-inner"]/h1/text()'))
        title = re.sub(r'[\\/*?:"<>|]', '', title)
        title = title.strip(' .')
        logger.debug(f"解析到视频标题: {title}")
        
        soup = BeautifulSoup(text, 'html.parser')
        for script in soup.find_all('script'):
            script_text = script.string
            if not script_text:
                continue
                
            # 提取 playinfo 数据
            if 'window.__playinfo__' in script_text:
                match = re.search(
                    r'window\.__playinfo__\s*=\s*({.*?})(?:\s*;|\s*$)',
                    script_text,
                    re.DOTALL
                )
                if match and len(match.group(1)) > 8000:
                    json_str = match.group(1)
                    data = json.loads(json_str)
                    logger.debug("成功解析 playinfo 数据")
                    
            # 提取 initial state 数据
            if 'window.__INITIAL_STATE__' in script_text:
                match = re.search(
                    r'window\.__INITIAL_STATE__\s*=\s*({.*?})(?:\s*;|\s*$)',
                    script_text,
                    re.DOTALL
                )
                if match and len(match.group(1)) > 8000:
                    json_str = match.group(1)
                    data1 = json.loads(json_str)
                    logger.debug("成功解析 initial state 数据")

            # 如果两个数据都已找到，可以提前返回
            if data and data1:
                return data, data1
    except Exception as e:
        logger.error(f"解析视频信息时出错: {str(e)}")
        
    return data, data1

def find_highest_quality_file_index(video_list: List[Dict[str, Any]]) -> int:
    """查找具有最高质量的媒体文件索引
    
    按照以下优先级排序：
    1. 宽度（分辨率）
    2. 高度（分辨率）
    3. 帧率
    4. 带宽
    
    Args:
        video_list: 包含媒体文件信息的列表
        
    Returns:
        具有最高质量的文件索引，如果列表为空则返回 -1
    """
    if not video_list:
        logger.debug("媒体列表为空")
        return -1
        
    highest_index = 0
    for index, video in enumerate(video_list):
        current_width = video.get('width', 0)
        current_height = video.get('height', 0)
        current_frame_rate = video.get('frameRate', video.get('frame_rate', 0))
        current_bandwidth = video.get('bandwidth', 0)
        
        highest_video = video_list[highest_index]
        highest_width = highest_video.get('width', 0)
        highest_height = highest_video.get('height', 0)
        highest_frame_rate = highest_video.get('frameRate', highest_video.get('frame_rate', 0))
        highest_bandwidth = highest_video.get('bandwidth', 0)
        
        if (current_width > highest_width or
                (current_width == highest_width and current_height > highest_height) or
                (current_width == highest_width and current_height == highest_height and 
                 current_frame_rate > highest_frame_rate) or
                (current_width == highest_width and current_height == highest_height and 
                 current_frame_rate == highest_frame_rate and current_bandwidth > highest_bandwidth)):
            highest_index = index
            
    logger.debug(f"找到最高质量媒体，索引: {highest_index}")
    return highest_index

def get_media_info(bv_json: Dict[str, Any]) -> Dict[str, Any]:
    """从 BV JSON 中获取最高质量的音频信息
    
    首先尝试获取 FLAC 格式音频，如果不可用则回退到普通音频格式
    
    Args:
        bv_json: 包含媒体信息的JSON数据
        
    Returns:
        包含音频链接、格式和是否为FLAC的字典
    """
    audio_list = []
    
    try:
        # 尝试获取 FLAC 音频
        logger.debug("尝试获取FLAC音频")
        flac_data = bv_json.get("data", {}).get("dash", {}).get("flac", {}).get("audio", {})
        if flac_data is not None and flac_data.get("baseUrl"):
            codecs = flac_data.get("codecs", "")
            logger.info("找到FLAC格式音频")
            return {
                "link": flac_data["baseUrl"],
                "audio_format": codecs.lower(),
                "is_flac": True
            }
    except Exception as e:
        # 如果没有 FLAC，回退到普通音频
        audio_list = bv_json.get("data", {}).get("dash", {}).get("audio", [])
        
    # 如果没有找到音频列表或FLAC获取失败，尝试获取普通音频
    if not audio_list:
        logger.debug("尝试获取普通音频")
        audio_list = bv_json.get("data", {}).get("dash", {}).get("audio", [])
        
    if not audio_list:
        logger.error("未找到任何音频流")
        return {"link": "", "audio_format": "", "is_flac": False}
        
    highest_index = find_highest_quality_file_index(audio_list)
    if highest_index < 0:
        logger.error("音频列表为空或无法找到最高质量音频")
        return {"link": "", "audio_format": "", "is_flac": False}
        
    max_audio = audio_list[highest_index]
    codecs = max_audio.get('codecs', "")
    audio_format = codecs.split('.')[0].lower() if codecs else ""
    
    logger.info(f"找到最高质量音频: 格式={audio_format}, 带宽={max_audio.get('bandwidth', 'unknown')}")
    
    return {
        "link": max_audio.get('baseUrl', ""),
        "audio_format": audio_format,
        "is_flac": False
    }

def extract_bvid(url_or_bvid: str) -> Optional[str]:
    """从URL或字符串中解析B站视频的BV号。

    Args:
        url_or_bvid: B站视频链接或BV号。

    Returns:
        如果成功解析，返回BV号字符串；否则返回None。
    """
    # 正则表达式匹配BV号 (BV + 10个字母数字)
    match = re.search(r'(BV[a-zA-Z0-9]{10})', url_or_bvid)
    if match:
        return match.group(1)
    return None

def fetch_video_info(link, max_attempts=10, delay=5):
    """获取视频信息，包括媒体文件链接和格式。    
    如果请求失败，将重试最多 max_attempts 次，每次重试之间等待 delay 秒。
    
    Args:
        link: 视频链接。
        max_attempts: 最大重试次数。
        delay: 重试之间的等待时间（秒）。
    
    Returns:
        如果请求成功，返回包含媒体文件链接和格式的字典；否则返回None。
    """
    
    logger.info(f"正在获取视频信息: {link}")
    media_info_link = None
    media_info_meta = None

    for attempt in range(max_attempts):
        logger.info(f"尝试获取视频信息 (第 {attempt + 1}/{max_attempts} 次)")
        s = requests.Session()
        
        # 设置请求头
        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "accept-language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
            "cache-control": "max-age=0",
            "priority": "u=0, i",
            "referer": "https://www.bilibili.com/?spm_id_from=333.788.0.0",
            "sec-ch-ua": "\"Chromium\";v=\"136\", \"Google Chrome\";v=\"136\", \"Not.A/Brand\";v=\"99\"",
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "\"Windows\"",
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "same-origin",
            "sec-fetch-user": "?1",
            "upgrade-insecure-requests": "1",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
        }
        
        # 使用随机用户代理
        ua = UserAgent().random
        headers.update({
            "user-agent": ua,
            "sec-ch-ua": get_sec_ch_ua(ua),
            "sec-ch-ua-platform": f"\"{get_platform(ua)}\"",
            "sec-ch-ua-mobile": get_sec_ch_ua_mobile(ua),
        })
        logger.debug(f"使用随机用户代理: {ua}")
            
        s.headers = headers
        
        try:
            # 获取视频页面
            r = s.get(url=link, timeout=30)
            r.raise_for_status()
            
            # 解析视频信息
            media_info_link, media_info_meta = parse_bv_info(r.text)
            
            # 检查是否为充电专属视频
            if (media_info_meta and 
                media_info_meta.get('video') and 
                media_info_meta.get('video').get('viewInfo') and 
                media_info_meta.get('video').get('viewInfo').get('is_upower_exclusive')):
                logger.warning("视频为充电专属，跳过下载")
                return 'excluded', "", None

            # 检查是否获取到视频信息
            if media_info_link:
                logger.info(f"成功获取视频信息 (第 {attempt + 1} 次尝试)")
                audio_info = get_media_info(media_info_link)
                audio_link = audio_info.get("link")
                audio_json = {
                    "title":media_info_meta.get('videoData').get('title'),
                    "owner":media_info_meta.get('videoData').get('owner').get('name'),
                    "datetime":media_info_meta.get('videoData').get('ctime'),
                    "bvid":bvid,
                    "duration":media_info_meta.get("videoData").get("duration")}

                return "ok", audio_link, audio_json
                                
            logger.warning(f"第 {attempt + 1} 次尝试失败，未获取到媒体信息")
            time.sleep(delay)
            
        except requests.exceptions.RequestException as e:
            logger.error(f"请求失败: {e}")
            time.sleep(delay)
    
    logger.error(f"所有 {max_attempts} 次尝试均失败，无法获取视频信息")
    return "failed", "", None

def download_audio(audio_link, output_filename: Path, max_attempts=10, delay=5) -> str:
    for attempt in range(max_attempts):
        try:
            logger.info(f"开始下载音频: {audio_link}")

            r = s.get(audio_link, stream=True, timeout=60)
            r.raise_for_status()
            
            # 获取文件大小用于进度条
            total_size = int(r.headers.get('content-length', 0))
            
            # 确保输出目录存在
            output_filename.parent.mkdir(parents=True, exist_ok=True)
    
            # 使用tqdm显示下载进度
            with open(output_filename, 'wb') as f, tqdm(
                desc="下载音频",
                total=total_size,
                unit='B',
                unit_scale=True,
                unit_divisor=1024,
            ) as bar:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:  # 过滤掉保持连接活跃的空块
                        f.write(chunk)
                        bar.update(len(chunk))
            
            logger.info("音频下载成功")
        except requests.exceptions.RequestException as e:
            logger.error(f"请求错误: {str(e)}")
            time.sleep(delay)
            continue
        except Exception as e:
            logger.error(f"下载过程中出错: {str(e)}")
            time.sleep(delay)
            continue
    else:
        # 所有尝试都失败
        logger.error(f"在 {max_attempts} 次尝试后仍未能下载音频")
        return 'failed'

def fetch_audio_link_from_line(line: str) -> tuple(str, str, Optional(dict)):
   # 解析BV号
    bvid = extract_bvid(line)
    if not bvid:
        logger.error(f"未能从 '{line}' 中解析到有效的BV号")
        return 'failed', "", None

    link = f"https://www.bilibili.com/video/{bvid}"
    logger.info(f"解析到BV号: {bvid}, 构造请求链接: {link}")

    # 获取视频信息
    max_attempts = 10
    delay = 5
    return fetch_video_info(link, max_attempts, delay)

def download_audio_from_line(link: str, output_dir: Path, max_duration: int) -> str:
    """下载B站视频的音频
    
    Args:
        link: B站视频链接或BV号
        output_dir: 输出目录
        max_duration: 最大下载时长（秒），可选
        
    Returns:
        状态码: 'ok' 表示成功，'excluded' 表示视频不可下载
    """
    # 获取视频信息
    max_attempts = 10
    delay = 5
    status, audio_link, audio_json = fetch_audio_link_from_line(link, max_attempts, delay)
    
    if status == 'ok':
        logger.info(f"成功获取视频信息: {audio_link}")
                            
        # 设置输出文件名
        output_filename = output_dir / "audio.mp3"

        # 下载音频
        status = download_audio(audio_link, output_filename)
            
        if status == 'ok':
            with open(output_filename.with_suffix('.json'), 'w', encoding='utf-8') as f:
                json.dump(audio_json, f, ensure_ascii=False)                
            logger.info("生成音频json成功")
                
            # 下载成功，跳出循环
            break
            
    return status

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="下载B站视频的音频")
    parser.add_argument("link", help="B站视频链接或BV号")
    parser.add_argument("-o", "--output", type=str, default=".", help="输出目录 (默认: 当前目录)")
    parser.add_argument("-d", "--debug", action="store_true", help="启用调试日志")
    parser.add_argument("-m", "--max-duration", type=int, default=0, help="最大下载时长（秒）")
    parser.add_argument("-r", "--dry-run", action="store_true", help="仅打印要下载的音频信息")
    
    args = parser.parse_args()
    
    # 设置日志级别
    if args.debug:
        logger.setLevel(logging.DEBUG)
        logger.debug("调试模式已启用")
    
    # 创建输出目录
    output_path = Path(args.output)
    
    try:
        result = download_audio_from_line(args.link, output_path)
        if result == 'ok':
            logger.info(f"下载完成，文件保存在: {output_path.absolute()}")
            sys.exit(0)
        elif result == 'excluded':
            logger.error("该视频为充电专属，无法下载")
            sys.exit(2)
        else:
            logger.error("下载失败")
            sys.exit(1)
    except KeyboardInterrupt:
        logger.info("下载已取消")
        sys.exit(130)
    except Exception as e:
        logger.exception(f"发生错误: {str(e)}")
        sys.exit(1)