/* UI locale extensions. No network, radio-language change or automatic setup.
 * Explicit English fallback is not counted as a completed translation. */
(function () {
  const i18n = window.BasswiesnI18n;
  const fields = ["setup", "radios", "favorites", "settings", "diagnostics", "lab", "scan", "verify", "apply", "complete", "success", "failed", "presets", "sources", "volume", "playing", "battery", "capabilities", "runtime_state", "support_bundle", "logs", "status", "cloud", "ssh", "remote_services", "language", "guided_hints", "write_guard", "theme", "preset_checker", "provider_status", "refresh", "load_status", "download", "unknown", "loading", "retry", "error", "match", "different", "missing", "unreadable", "details", "start", "remote_control", "stations", "multiroom", "alarms", "device_settings", "more", "about_basswiesn", "name", "radio", "station", "overview", "devices_overview", "playback_log", "playback_statistics", "webgui_defaults", "safe_startup_volume", "default_timezone", "default_device_language", "save_settings", "setup_selected_radios", "setup_cancel", "radios_search", "configured_radios", "add_radio", "current_slots", "update_from_radio", "online_station_search", "search", "copy_presets", "copy_all_slots", "common_listening", "master_radio", "additional_rooms", "start_multiroom", "clear_groups", "current_state", "read_status", "telnet_reboot", "check_capability", "standby_clock_restore", "job_status", "cleanup", "clear_logs", "remove_test_devices", "battery_patch_title", "patch_apply", "rollback", "show_plan", "manual_only", "music_library", "backup_title", "config_title", "protocol_title", "reload", "target_radio", "status_unknown"];
  const rows = {
    ko: ["설정 시작", "라디오", "프리셋", "설정", "진단", "실험실", "검색", "확인", "적용", "완료", "성공", "실패", "프리셋", "소스", "음량", "재생 중", "배터리", "지원 기능", "실행 상태", "지원 자료", "로그", "상태", "클라우드", "SSH", "원격 서비스", "언어", "도움말", "쓰기 보호", "테마", "프리셋 검사", "제공업체 상태", "새로 고침", "상태 불러오기", "다운로드", "알 수 없음", "불러오는 중 …", "다시 시도", "오류", "일치", "다름", "없음", "읽을 수 없음", "상세 정보", "시작", "리모컨", "방송국", "멀티룸", "알람 및 타이머", "기기 설정", "더 보기", "BASSWIESN 소개", "이름", "라디오", "방송국", "개요", "기기", "재생 기록", "재생 통계", "웹 인터페이스 기본 설정", "안전한 시작 음량", "기본 시간대", "기본 기기 언어", "설정 저장", "선택한 라디오 설정 시작", "설정 취소", "라디오 검색", "등록된 라디오", "라디오 추가", "현재 프리셋", "라디오에서 새로 읽기", "온라인 방송국 검색", "검색", "프리셋 복사", "모든 프리셋 복사", "함께 듣기", "주 라디오", "추가 방", "멀티룸 시작", "선택한 그룹 해제", "현재 상태", "상태 읽기", "Telnet으로 기기 재시작", "지원 기능 확인", "대기 모드 시계 다시 켜기", "작업 상태", "정리", "로그 비우기", "테스트 기기 제거", "BatteryMonitor 패치 / Portable 배터리", "패치 적용", "되돌리기", "계획 보기", "수동", "음악 라이브러리", "백업", "기술 설정", "로그", "다시 불러오기", "대상 라디오", "상태를 알 수 없음"],
    th: ["ตั้งค่าเริ่มต้น", "วิทยุ", "สถานีที่บันทึกไว้", "การตั้งค่า", "การวินิจฉัย", "ห้องทดลอง", "ค้นหา", "ตรวจสอบ", "นำไปใช้", "เสร็จสิ้น", "สำเร็จ", "ไม่สำเร็จ", "สถานีที่บันทึกไว้", "แหล่งเสียง", "ระดับเสียง", "กำลังเล่น", "แบตเตอรี่", "ความสามารถ", "สถานะการทำงาน", "ชุดข้อมูลสนับสนุน", "บันทึก", "สถานะ", "คลาวด์", "SSH", "บริการระยะไกล", "ภาษา", "คำแนะนำ", "ป้องกันการเขียน", "ธีม", "ตรวจสอบสถานีที่บันทึกไว้", "สถานะผู้ให้บริการ", "รีเฟรช", "โหลดสถานะ", "ดาวน์โหลด", "ไม่ทราบ", "กำลังโหลด …", "ลองอีกครั้ง", "ข้อผิดพลาด", "ตรงกัน", "แตกต่าง", "ไม่มี", "อ่านไม่ได้", "แสดงรายละเอียด", "เริ่ม", "รีโมต", "สถานี", "หลายห้อง", "นาฬิกาปลุกและตัวตั้งเวลา", "การตั้งค่าอุปกรณ์", "เพิ่มเติม", "เกี่ยวกับ BASSWIESN", "ชื่อ", "วิทยุ", "สถานี", "ภาพรวม", "อุปกรณ์", "ประวัติการเล่น", "สถิติการเล่น", "ค่าเริ่มต้นของเว็บ", "ระดับเสียงเริ่มต้นที่ปลอดภัย", "เขตเวลาเริ่มต้น", "ภาษาเริ่มต้นของอุปกรณ์", "บันทึกการตั้งค่า", "เริ่มตั้งค่าวิทยุที่เลือก", "ยกเลิกการตั้งค่า", "ค้นหาวิทยุ", "วิทยุที่ลงทะเบียน", "เพิ่มวิทยุ", "สถานีที่บันทึกไว้ปัจจุบัน", "อ่านจากวิทยุใหม่", "ค้นหาสถานีออนไลน์", "ค้นหา", "คัดลอกสถานีที่บันทึกไว้", "คัดลอกทุกช่อง", "ฟังด้วยกัน", "วิทยุหลัก", "ห้องเพิ่มเติม", "เริ่มเล่นหลายห้อง", "ยุบกลุ่มที่เลือก", "สถานะปัจจุบัน", "อ่านสถานะ", "เริ่มอุปกรณ์ใหม่ผ่าน Telnet", "ตรวจสอบความสามารถ", "เปิดนาฬิกาโหมดพักอีกครั้ง", "สถานะงาน", "ล้างข้อมูล", "ล้างบันทึก", "ลบอุปกรณ์ทดสอบ", "แพตช์ BatteryMonitor / แบตเตอรี่ Portable", "ใช้แพตช์", "ย้อนกลับ", "แสดงแผน", "ด้วยตนเอง", "คลังเพลง", "สำรองข้อมูล", "การตั้งค่าทางเทคนิค", "บันทึก", "โหลดใหม่", "วิทยุเป้าหมาย", "ไม่ทราบสถานะ"],
    "zh-Hant": ["初始設定", "收音機", "預設電台", "設定", "診斷", "實驗室", "掃描", "驗證", "套用", "完成", "成功", "失敗", "預設電台", "音源", "音量", "播放中", "電池", "支援功能", "執行狀態", "支援資料包", "記錄", "狀態", "雲端", "SSH", "遠端服務", "語言", "操作提示", "寫入保護", "佈景主題", "預設電台檢查", "供應商狀態", "重新整理", "載入狀態", "下載", "未知", "載入中 …", "重試", "錯誤", "相符", "不同", "缺少", "無法讀取", "顯示詳細資料", "開始", "遙控器", "電台", "多房間", "鬧鐘與計時器", "裝置設定", "更多", "關於 BASSWIESN", "名稱", "收音機", "電台", "總覽", "裝置", "播放記錄", "播放統計", "網頁介面預設值", "安全啟動音量", "預設時區", "預設裝置語言", "儲存設定", "開始設定所選收音機", "取消設定", "搜尋收音機", "已登記的收音機", "新增收音機", "目前預設電台", "從收音機重新讀取", "線上電台搜尋", "搜尋", "複製預設電台", "複製所有預設電台", "一起聆聽", "主收音機", "其他房間", "啟動多房間播放", "解散所選群組", "目前狀態", "讀取狀態", "透過 Telnet 重新啟動裝置", "檢查支援功能", "重新啟用待機時鐘", "工作狀態", "清理", "清除記錄", "移除測試裝置", "BatteryMonitor 修補程式 / Portable 電池", "套用修補程式", "還原", "顯示計畫", "手動", "音樂資料庫", "備份", "技術設定", "記錄", "重新載入", "目標收音機", "狀態未知"]
  };
  const safetyFields = ["first_run_title", "first_run_p1", "first_run_p2", "first_run_p3", "first_run_p4", "first_run_read", "first_run_never", "first_run_ack"];
  const safety = {
    ko: ["사용에 따른 위험은 사용자 책임입니다", "BASSWIESN은 SoundTouch 기기의 설정을 변경하며, 특정 상황에서는 기기가 손상될 수 있습니다.", "라디오 설정, 프리셋, Telnet 재시작 및 복구 작업으로 재시작이나 영구적인 변경이 발생할 수 있습니다. 실제 쓰기 작업은 사용자의 명시적인 승인 후에만 수행됩니다.", "개인이 개발한 미완성 소프트웨어입니다. 사용에 따른 위험은 사용자 책임입니다.", "바이에른에서 인사드립니다", "안내를 읽었습니다", "다시 표시하지 않기", "확인"],
    th: ["ใช้งานโดยยอมรับความเสี่ยงเอง", "BASSWIESN สามารถเปลี่ยนการตั้งค่าอุปกรณ์ SoundTouch และอาจทำให้อุปกรณ์เสียหายได้ในบางกรณี", "การตั้งค่าวิทยุ สถานีที่บันทึกไว้ การเริ่มใหม่ผ่าน Telnet และการกู้คืน อาจทำให้เครื่องเริ่มใหม่หรือเกิดการเปลี่ยนแปลงถาวร การเขียนข้อมูลจริงจะทำหลังจากผู้ใช้อนุมัติอย่างชัดเจนเท่านั้น", "ซอฟต์แวร์นี้พัฒนาโดยบุคคลทั่วไปและยังไม่เสร็จสมบูรณ์ ผู้ใช้ต้องยอมรับความเสี่ยงเอง", "คำทักทายจากบาวาเรีย", "ฉันอ่านคำเตือนแล้ว", "ไม่ต้องแสดงอีก", "ยืนยัน"],
    "zh-Hant": ["使用風險由使用者自行承擔", "BASSWIESN 可變更 SoundTouch 裝置設定，在某些情況下可能損壞裝置。", "收音機設定、預設電台、Telnet 重新啟動與復原操作可能導致重新啟動或永久變更。實際寫入操作僅在使用者明確同意後執行。", "本軟體由個人開發，尚未完成。使用風險由使用者自行承擔。", "來自巴伐利亞的問候", "我已閱讀警告", "不再顯示", "確認"]
  };
  const notices = {
    en: "This language is partly translated. Untranslated controls and explanations use English; changing the interface language does not change the radio language.",
    ko: "이 언어는 일부만 번역되어 있습니다. 번역되지 않은 버튼과 설명은 영어로 표시됩니다. 화면 언어를 변경해도 라디오의 언어는 바뀌지 않습니다.",
    th: "ภาษานี้แปลแล้วบางส่วน ปุ่มและคำอธิบายที่ยังไม่แปลจะแสดงเป็นภาษาอังกฤษ การเปลี่ยนภาษาหน้าเว็บไม่เปลี่ยนภาษาของวิทยุ",
    "zh-Hant": "此語言目前僅部分翻譯。未翻譯的控制項與說明會使用英文。變更介面語言不會變更收音機語言。"
  };
  for (const [language, values] of Object.entries(rows)) {
    if (values.length !== fields.length || safety[language].length !== safetyFields.length) throw new Error("Invalid locale row length");
    i18n.catalogs[language] = {...i18n.catalogs.en,
      ...Object.fromEntries(fields.map((key, index) => [key, values[index]])),
      ...Object.fromEntries(safetyFields.map((key, index) => [key, safety[language][index]]))};
    if (!i18n.languages.includes(language)) i18n.languages.push(language);
  }
  function normalize(language) {
    const parts = String(language || "").trim().toLowerCase().split(/[.@;]/)[0].replaceAll("_", "-").split("-");
    if (parts[0] === "zh") return parts.includes("hant") || (!parts.includes("hans") && parts.some(p => ["tw", "hk", "mo"].includes(p))) ? "zh-Hant" : "zh";
    const code = parts[0] === "nb" ? "no" : parts[0];
    return i18n.catalogs[code] ? code : "en";
  }
  let current = "en";
  const originalSet = i18n.setLanguage, originalPhrase = i18n.phrase, originalDynamic = i18n.dynamic;
  i18n.setLanguage = language => { current = normalize(language); originalSet(current); };
  i18n.normalizeLanguage = normalize;
  // Existing phrase aliases also contain version-specific keys. Map exact
  // core phrases to these native labels without substring replacement.
  const phraseKeys = new Map(), nativeKeys = new Map();
  for (const key of [...fields, ...safetyFields]) {
    for (const language of ["de", "en", ...Object.keys(rows)]) {
      phraseKeys.set(i18n.catalogs[language][key].trim().toLowerCase(), key);
      if (rows[language]) nativeKeys.set(i18n.catalogs[language][key].trim().toLowerCase(), key);
    }
  }
  const nativePhrase = value => {
    const key = (rows[current] ? phraseKeys : nativeKeys).get(String(value || "").trim().toLowerCase());
    return key ? i18n.catalogs[current][key] : value;
  };
  i18n.phrase = value => { const translated = nativePhrase(value); return translated !== value ? translated : originalPhrase(value); };
  i18n.dynamic = value => { const translated = nativePhrase(value); return translated !== value ? translated : nativePhrase(originalDynamic(value)); };
  i18n.languageNotice = language => ["de", "en"].includes(normalize(language)) ? "" : notices[normalize(language)] || notices.en;
  i18n.coverage = language => {
    const code = normalize(language), keys = Object.keys(i18n.catalogs.en);
    return {language: code, keys: keys.length,
      missing: keys.filter(key => !i18n.catalogs[code][key]),
      sameAsEnglish: keys.filter(key => i18n.catalogs[code][key] === i18n.catalogs.en[key]).length,
      note: "String equality includes protocol terms; it is not a native-translation percentage."};
  };
  const about = window.BasswiesnAbout;
  if (about) for (const language of Object.keys(rows)) {
    about.labels[language] = {...about.labels.en, title: i18n.catalogs[language].about_basswiesn};
    about.fallbackNotices[language] = {
      ko: "작성자의 글은 영어로 표시됩니다. 독일어 원문은 변경되지 않았습니다.",
      th: "ข้อความของผู้เขียนแสดงเป็นภาษาอังกฤษ ต้นฉบับภาษาเยอรมันไม่ได้เปลี่ยนแปลง",
      "zh-Hant": "作者的文章以英文顯示，德文原文保持不變。"
    }[language];
  }
}());
