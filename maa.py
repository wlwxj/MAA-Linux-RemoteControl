from pathlib import Path
import os
import json
import time
import datetime
import yaml
import uuid
import traceback
import subprocess
import base64
from loguru import logger as lg
import websocket
from aiohttp.client_ws import ClientWebSocketResponse
import gc
import uuid
from threading import Thread


lg.add(str(Path(__file__).parent / f"logs/log_{time.strftime('%Y-%m-%d', time.localtime()) }.log"), rotation="1 day")

tasks_config = []
my_maa = None
wsapp = None






from asst.asst import Asst
from asst.utils import Message, Version, InstanceOptionType
from asst.updater import Updater
@Asst.CallBackType
def my_callback(msg, details, arg) -> None:
	"""
	MAA-CORE的运行消息回调函数
	:params:
		``msg``: 消息类型
		``details``:  消息具体内容
	:return: None
	"""
	m = Message(msg)
	js = json.loads(details.decode('utf-8'))
	if m == Message.InternalError or m == Message.SubTaskError or m == Message.TaskChainError:
		text = f"MAA出错：{str(m)} {details.decode('utf-8')}"
		lg.error(text)
		# send_msg(text) #先不发送了，防止刷屏消息被封
		# if  maa.stop_tag != 'MAA出错':
		# 	maa.stop_tag = 'MAA出错'
		# 	maa.asst.stop()
		# exit(1)
	if m == Message.AsyncCallInfo:
		text = f"收到异步调用的回调消息：{str(m)} {details.decode('utf-8')}"
		# maa.waiting_async.remove(js['async_call_id'])
		lg.info(text)
		
	if 'what' in js:
		what = js['what']
		if what == 'UuidGot':
			text = f"获取到ADB设备uuid：{js['details']['uuid']}"
			lg.info(text)
			my_maa.connect_log += text + '\n'
			#mymaa 明明是在后面定义的呀，很怪，但是确实能跑（本代码依赖bug运行）
		elif what == 'ResolutionGot':
			text = f"获取到ADB设备分辨率：{js['details']['height']} X {js['details']['width']}"
			lg.info(text)
			my_maa.connect_log += text + '\n'
		elif what == 'StageDrops':
			my_maa.add_fight_msg(js['details'])
		elif what == 'ScreencapFailed' and my_maa.stop_tag != '需要重连':
			text = f"截图失败，可能是ADB配置出现问题或Android 11无线调试变化了端口，尝试重新连接"
			lg.info(text)
			my_maa.stop_tag = '需要重连'
			my_maa.asst.stop()
	if my_maa.asst_config['python']['debug']:
		lg.info(m)
		lg.info(js)


class MAA:
	def __init__(self) -> None:
		# self.waiting_async = []
		self.tasks_config = []
		# self.error_threshold = 5
		# self.error_count = 0
		self.connect_log = ''
		self.fight_log = {'stages':{},'drops':{},'msg':''}	#作战信息
		self.stop_tag = '正常'	#任务运行完成后会检查回调函数有没有给出报错，有的话重新执行
		#MAA核心路径
		self.core_path = Path(__file__).parent.parent / "MAA-linux"
		os.environ['LD_LIBRARY_PATH'] = str(self.core_path)
		#获取maa自定义配置
		self.asst_config_path = str(Path(__file__).parent / "config/asst.yaml")
		with open(self.asst_config_path, 'r', encoding='utf8') as config_f:
			self.asst_config = yaml.safe_load(config_f)
		#更新并加载核心和共享库
		self.update_and_load()

	def update_and_load(self):
		#更新版本
		self.update_log = Updater(self.core_path, Version.Beta).update()
		#添加自定义任务模块
		self.add_custom()
		#加载资源
		Asst.load(path=self.core_path, incremental_path=self.core_path / 'cache')
		#构造并设置回调函数
		self.asst = Asst(callback=my_callback)
		#设置触控方式
		self.asst.set_instance_option(InstanceOptionType.touch_type, self.asst_config['instance_options']['touch_mode'])

	def add_custom(self):
		"""
		将自定义的任务动作和修复问题的模板图片合并到官方的文件中
		"""
		custom_tasks_path = Path(__file__).parent / "custom/tasks.json"
		official_tasks_path = self.core_path / "resource/tasks.json"

		with open(custom_tasks_path, 'r', encoding='utf8') as file:
			customs_tasks = json.load(file)	#读取自定义任务动作
		with open(official_tasks_path, 'r', encoding='utf8') as file:
			official_tasks = json.load(file)	#读取官方任务动作
		for key, values in customs_tasks.items():
			official_tasks[key] = values	##自定义任务动作加入到官方配置并保存
		with open(official_tasks_path, 'w',encoding='utf8') as file:
			file.write(json.dumps(official_tasks, ensure_ascii=False, indent=4, separators=(', ', ': ')))

		custom_template_path = Path(__file__).parent / "custom/template"
		official_template_path = self.core_path / "resource/template"
		custom_template = os.listdir(custom_template_path) 
		for template in custom_template: 
			custom_template_file_path = os.path.join(custom_template_path, template) 
			official_template_file_path = os.path.join(official_template_path, template) 
			with open(custom_template_file_path, 'rb') as custom_f: 
				with open(official_template_file_path, 'wb') as official_f: 
						official_f.write(custom_f.read())



	def find_adb_wifi_port(self,retry=50):
		"""
		Android 11以上可以在开发人员选项内开启无线调试
		但是端口隔一段时间会变化，所以要扫描一下
		"""
		while retry:
			f=os.popen(f'nmap {self.asst_config["connection"]["ip"]} -p 30000-49999 | awk "/\\/tcp/" | cut -d/ -f1')  # 返回的是一个文件对象
			port = f.read().replace(' ','').replace('\n','')
			text = f"扫描得到ADB端口为：{port}"
			lg.info(text)
			if not port:
				text = "扫描不到设备ADB端口，等待重试"
				lg.error(text)
				time.sleep(5)
				retry -=1
			else:
				self.asst_config["connection"]["port"] = int(port)
				return int(port)
		text = "扫描不到设备ADB端口，不再重试，请排查"
		lg.error(text)
		return 0



	def connect(self, init=False, retry=0):
		"""
		通过ADB连接到安卓设备
		"""
		self.connect_log = f"第{retry}次尝试连接ADB"
		#获取ADB端口
		if ( init or retry ) and self.asst_config['connection']['scan_port']:
			if not self.find_adb_wifi_port():
				return False
		text = f'尝试连接到：{self.asst_config["connection"]["ip"]}:{self.asst_config["connection"]["port"]}'
		lg.info(text)
		self.connect_log += "\n" + text

		if self.asst.connect(	adb_path = self.asst_config["connection"]["adb"], 
								address = f'{self.asst_config["connection"]["ip"]}:{self.asst_config["connection"]["port"]}', 
								config = self.asst_config["connection"]["config"]
							):
			text = '连接成功'
			lg.info(text)
			self.connect_log += '\n' + text
			self.screenshot('连接成功后')
			return True
		else:
			if retry<10:
				text = f'连接失败，即将尝试重连'
				lg.error(text)
				return self.connect(retry=retry+1)
			else:
				text = f'连接失败，请检查MAA-CORE的日志，应当位于 {self.core_path}/debug/asst.log'
				lg.error(text)
				self.connect_log += text + '\n'
				return False

	def screenshot(self,file_name,rb=False): 
		"""
		调用MAA-CORE的API获取截图
		get_img这个不是立即截图，只是获取任务运行的最近一张截图，
		所以手动调用adb立即截图
		:params:
			``file_name``: 截图保存的文件名
			``rb``:  是否返回图片文件的二进制bytes数组
		:return: 图片文件bytes[] | None
		"""
		# async_call_id = self.asst.screenshot()
		# lg.info(f"将async_call_id：{async_call_id}加入到waiting_async队列中并等待")
		# self.waiting_async.append(async_call_id)
		# while async_call_id in self.waiting_async:
		# 	time.sleep(0)
		# lg.info(f"waiting_async结束，截图已完成")
		
		# img,length = self.asst.get_img()
		img_path = str(Path(__file__).parent / f"img/{file_name}.png")
		shell_cmd = f'{self.asst_config["connection"]["adb"]} -s '\
					f'{self.asst_config["connection"]["ip"]}:{self.asst_config["connection"]["port"]} '\
					f'exec-out screencap -p > '\
					f'{img_path}'
     
		pipe = subprocess.Popen(shell_cmd,
								stdin=subprocess.PIPE,
								stdout=subprocess.PIPE, shell=True)
		tmp = pipe.stdout.read().replace(b'\r\n', b'\n')	#直接读取输出有问题，还是先写到文件里吧
		with open(img_path, 'rb') as f:
			img = f.read()
		length = len(img)
		lg.info(f"截图大小{length/1024}KBytes")
		if rb:
			return img
		else:
			return length



	def add_fight_msg(self, detail):
		"""
		对回调函数收到的作战结果进行解析
		:params:
			``details``:  消息具体内容
		:return: None
		"""
		stage = detail["stage"]["stageCode"]
		if stage not in self.fight_log['stages']:
			self.fight_log['stages'][stage] = 1
		else:
			self.fight_log['stages'][stage] += 1
		for drop in detail['stats']:
			self.fight_log['drops'][drop['itemName']] = drop['quantity']
		self.fight_log['msg'] = '作战结果：\n'
		for key,value in self.fight_log['stages'].items():
			self.fight_log['msg'] += f"  {key} * {value}\n"
		self.fight_log['msg'] += '战斗掉落：\n'
		for key,value in self.fight_log['drops'].items():
			self.fight_log['msg'] += f"  {key} * {value}\n"
		while self.fight_log['msg'][-1:] == '\n':
			self.fight_log['msg'] = self.fight_log['msg'][:-1]


	def tasks_handler(self, data: dict):
		global wsapp
		config_name = data['name'] if 'name' in data else int(time.time())
		lg.info(f"正在运行配置：{config_name}")
		task_index = 0
		success_tasks = []
		while task_index < len(data['tasks']):
			lg.info(f"正在处理第{task_index}个任务")
			task = data['tasks'][task_index]
			lg.info(task)
			recall = {
						"devices": uuid.UUID(int = uuid.getnode()).hex[-12:].upper(),
						"user": "",
						"task": task['id'] if 'id' in task else f"{task['type']}_{task_index}",
						"task_type": task['type'],
						"status": "SUCCESS",
						"payload": "",
						"image": "",
						"type": "recall",
						"name": config_name
					}
			task_index += 1
			self.fight_log = {'stages':{},'drops':{},'msg':''}
			if task['type'] == 'Update':
				lg.info("收到更新任务，调用初始化函数")
				self.__init__()
				recall['payload'] = self.update_log
				wsapp.send(json.dumps(recall, ensure_ascii=False))
			else:
				lg.info("当前已执行了的核心任务")
				lg.info(success_tasks)
				if task_index-1 in success_tasks:
					if task_index-1 != success_tasks[-1]:
						lg.info("跳过非最后一个已执行了的任务")
						continue
				lg.info("运行任务前检查ADB连接情况")
				if not self.connect():
					lg.error("连接失败，放弃该配置的运行")
					recall['status'] = "False"
					recall['payload'] = self.connect_log
					wsapp.send(json.dumps(recall, ensure_ascii=False))
					break
				lg.info("检查完毕：ADB连接正常")
				lg.info("检查任务是否启用")
				if 'enable' in task and task['enable'] == False:
					lg.info("该任务未启用，跳过")
					continue
				if 'condition' in task:
					now = datetime.datetime.now()
					if 'weekday' in task['condition']:
						if now.weekday()+1 not in task['condition']['weekday']:
							lg.info("未达到该任务启用的星期范围，跳过")
							continue
					if 'hour' in task['condition']:
						if '<' in task['condition']['hour'] and  now.hour >= task['condition']['hour']['<']:
							lg.info("时刻超过任务启用的范围，跳过")
							continue
						if '>' in task['condition']['hour'] and  now.hour <= task['condition']['hour']['>']:
							lg.info("时刻未达任务启用的范围，跳过")
							continue
				lg.info("检查完毕：任务正常启用")
				lg.info("准备提交任务")
				if "params" in task:
					self.asst.append_task(task['type'], task['params'])
				else:
					self.asst.append_task(task['type'])
				lg.info("检查是否需要在运行前截图")
				img_msg = None
				if ("screenshot" in task and (task['screenshot'] == 'before' or task['screenshot'] =='both')):
					img_msg = self.screenshot(f"before_{recall['task']}",rb=True)
				lg.info("启动运行")
				self.asst.start()
				lg.info("循环等待任务结束")
				while self.asst.running():
					time.sleep(0)
				lg.info("任务结束运行")
				lg.info("检查结束标记")
				if self.stop_tag == 'MAA出错':
					text = f"任务运行过程中存在出错情况，具体问题定位有待代码完善"
					recall['payload'] += text + '\n'
					# self.error_count +=1
					# if self.error_count >= self.error_threshold: 
					# 	text = "出错次数超过阈值，退出程序"
					# 	lg.error(text)
					# 	send_msg(text)
					# 	exit(1)
					# else:
					# 	text = f"MAA出错，尝试重新回到第{latest_official_task_count}个任务{task_config['tasks'][latest_official_task_count]['type']}"
					# 	lg.error(text)
					# 	task_count = 0
					# 	self.stop_tag = '正常'
				recall['payload'] += self.fight_log['msg']
				if self.stop_tag == '需要重连':
					text = "任务运行过程出错，重新尝试连接到adb"
					lg.error(text)
					recall['payload'] += text + '\n'
					if not self.connect(init=True):
						text = "连接失败，放弃该配置的运行"
						lg.error(text)
						recall['status'] = "False"
						recall['payload'] += self.connect_log + "\n" + text
						wsapp.send(json.dumps(recall, ensure_ascii=False))
						break
					self.stop_tag = '正常'
					task_index = 0
					text = f"MAA重连成功，尝试重新执行未完成的非custom非启闭任务，重置task_index到0"
					lg.error(text)
					recall['payload'] += text
					recall['status'] = "FAILED"
					wsapp.send(json.dumps(recall, ensure_ascii=False))
					continue
				lg.info("检查是否需要在运行后截图")
				if "screenshot" in task and (task['screenshot'] == 'after' or task['screenshot'] =='both'):
					img_msg = self.screenshot(f"after_{recall['task']}",rb=True)

				if task['type'] != 'Custom' and task['type'] != 'CloseDown' and task['type'] != 'StartUp':
					success_tasks.append(task_index-1)
					text = f"更新最近一个已完成的非custom非启闭任务为：{recall['task']}，序号为{task_index-1}"
					lg.info(text)
					lg.info(success_tasks)
				if img_msg:
					lg.info("将截图填入回调消息中")
					recall['image'] = base64.b64encode(img_msg).decode("utf-8")

				lg.info("发送回调消息")
				wsapp.send(json.dumps(recall, ensure_ascii=False))

				lg.info("清理任务队列，等待后续任务执行")
				self.asst.stop()



def handle_tasks_config():
	global my_maa
	global tasks_config
	lg.info("启用配置队列轮询处理")
	sleep_state =True
	while True:
		try:
			time.sleep(10)
			if len(tasks_config):
				if sleep_state or my_maa == None:
					lg.info("激活MAA")
					my_maa = MAA()
					my_maa.connect(init=True)
					sleep_state =False
				lg.info("还有尚未完成的任务配置，将队列中的第一个提交到处理函数中")
				my_maa.tasks_handler(data=tasks_config[0]['data'])
				lg.info("该配置处理完成，将其清理出队列")
				tasks_config.pop(0)
				lg.info(f"队列中剩余{len(tasks_config)}个任务配置")
			else:
				if not sleep_state:
					lg.info(f"任务配置队列已全部处理完")
					lg.info(f"清理内存，删除MAA实例，进入休眠模式")
					del my_maa
					gc.collect()
					sleep_state = True
					my_maa = None
		except Exception as e:
			lg.error(traceback.format_exc())



def on_open(wsapp):
    lg.info("MAA成功连接到WS服务端")
def on_message(wsapp, msg):
	lg.info("收到WS消息:")
	lg.info(msg)
	try:
		data = json.loads(msg)
	except ValueError:
		wsapp.send(json.dumps({'type':'receipt','payload':'收到的消息无法通过json格式化'}, ensure_ascii=False))
	tasks_config.append({"data":data,"ws":wsapp})
	recall = {
			"status": "SUCCESS",
			"payload": "MAA已收到一条任务配置，加入任务处理队列中逐个运行",
			"type": "receipt",
		}
	wsapp.send(json.dumps(recall, ensure_ascii=False))
def on_error(wsapp, e):
	lg.error(f"WS连接出错 {e}")
def on_close(wsapp, close_status_code, close_reason):
	lg.info(f"WS连接关闭 {close_status_code} {close_reason}")
def ws_client():
	global wsapp
	while True:
		try:
			wsapp = websocket.WebSocketApp("ws://192.168.31.111:8068/maa",
									on_open=on_open,
									on_message=on_message,
									on_error=on_error,
									on_close=on_close)
			wsapp.run_forever()
		except Exception as e:
			lg.error(traceback.format_exc())
		finally:
			time.sleep(5)



# 创建 Thread 实例
WS客户端线程 = Thread(target=ws_client, args=())
MAA任务配置处理队列 = Thread(target=handle_tasks_config, args=())

# 启动线程运行
WS客户端线程.start()
MAA任务配置处理队列.start()

# 等待所有线程执行完毕
WS客户端线程.join()
MAA任务配置处理队列.join()
