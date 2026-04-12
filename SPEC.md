# MedReminder - 服藥提醒器APP開發規格

## 專案概述
- **專案名稱**: med_reminder
- **目標平台**: Android + iOS（雙系統）
- **框架**: Flutter 3.x

## 功能清單

### 個人服藥管理
- 藥品管理：新增/編輯/刪除藥品（名稱、劑量、單位）
- 藥品相片：拍攝或從相簿選擇
- OCR說明：Google ML Kit自動提取藥品說明文字
- 提醒設定：每日服藥時間、重複天數
- 本地通知：時區感知排程提醒
- 服藥記錄：標記已服用/忘記服用
- 服藥統計：圓環圖显示遵從率

### 家人共享
- P2P連線：WiFi Direct/同一WiFi網絡
- 家人上限：最多16位
- 藥品權限：私人/共享
- 共享藥品清單：家人可查看選擇
- 一鍵提醒：選擇家人發送服藥提醒
- 系統通知呈現：顯示藥品名稱+劑量

## 技術架構

### 依賴套件
```yaml
dependencies:
  provider: ^6.1.2
  hive: ^2.2.3
  hive_flutter: ^1.1.0
  flutter_local_notifications: ^18.0.1
  timezone: ^0.10.0
  google_mlkit_text_recognition: ^0.14.0
  image_picker: ^1.1.2
  permission_handler: ^11.3.1
  uuid: ^4.5.1
  intl: ^0.20.2
  path_provider: ^2.1.5
  fl_chart: ^0.70.2
```

## 數據模型
- Medication (typeId: 0)
- Reminder (typeId: 1)
- FamilyMember (typeId: 2)

## 構建問題
需要先安裝JDK才能構建APK。請從以下連結下載：
https://www.oracle.com/technetwork/java/javase/downloads/