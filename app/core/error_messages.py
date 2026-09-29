from __future__ import annotations


class Common:
    RESOURCE_ALREADY_EXISTS = "ข้อมูลนี้มีอยู่ในระบบแล้ว"
    REFERENCED_RESOURCE_NOT_FOUND = "ข้อมูลที่อ้างอิงไม่มีอยู่ในระบบ"
    CONSTRAINT_VIOLATION = "ข้อมูลไม่ผ่านเงื่อนไขของระบบ"
    INTERNAL_SERVER_ERROR = "เกิดข้อผิดพลาดภายในระบบ"
    TOO_MANY_REQUESTS = "มีการเรียกใช้งานถี่เกินไป กรุณาลองใหม่ภายหลัง"
    INSUFFICIENT_PERMISSIONS = "สิทธิ์ไม่เพียงพอ"
    VILLAGE_ID_NOT_ALLOWED_FOR_ROLE = "ไม่สามารถระบุ village_id สำหรับสิทธิ์การใช้งานนี้ได้"
    VILLAGE_ID_REQUIRED_SUPERADMIN = "ต้องระบุ village_id สำหรับ superadmin"
    VILLAGE_ACCESS_DENIED = "ไม่มีสิทธิ์เข้าถึงข้อมูลของหมู่บ้านนี้"


class Auth:
    COULD_NOT_VALIDATE_CREDENTIALS = "ไม่สามารถยืนยันตัวตนได้"
    ACCOUNT_INACTIVE = "บัญชีผู้ใช้งานถูกระงับการใช้งาน"
    ACCOUNT_NOT_VERIFIED = "บัญชีผู้ใช้งานยังไม่ได้ยืนยันตัวตน"
    VILLAGE_INACTIVE = "หมู่บ้านถูกระงับการใช้งาน"
    INVALID_API_KEY = "API key ไม่ถูกต้อง"
    INVALID_CREDENTIALS = "ชื่อผู้ใช้งานหรือรหัสผ่านไม่ถูกต้อง"
    INVALID_OR_EXPIRED_REFRESH_TOKEN = "เซสชันหมดอายุ กรุณาเข้าสู่ระบบใหม่อีกครั้ง"
    MISSING_REFRESH_TOKEN = "ไม่พบ refresh token"
    CURRENT_PASSWORD_INCORRECT = "รหัสผ่านปัจจุบันไม่ถูกต้อง"
    INVALID_OR_EXPIRED_TOKEN = "ลิงก์นี้หมดอายุ หรือไม่ถูกต้อง กรุณาทำรายการใหม่อีกครั้ง"
    SESSION_REVOKED_PASSWORD_CHANGED = "เซสชันถูกยกเลิกเนื่องจากมีการเปลี่ยนรหัสผ่าน กรุณาเข้าสู่ระบบใหม่"


class UserErrors:
    NOT_FOUND = "ไม่พบผู้ใช้งาน"
    SCOPE_ADMIN_ONLY_USER = "เฉพาะ superadmin เท่านั้นที่สามารถแก้ไขบัญชี admin หรือ superadmin ได้"
    SCOPE_OUTSIDE_VILLAGE = "ไม่สามารถจัดการผู้ใช้งานนอกหมู่บ้านของคุณได้"
    CANNOT_RESET_OWN_PASSWORD = "ไม่สามารถรีเซ็ตรหัสผ่านของตัวเองที่นี่ได้ กรุณาใช้ฟังก์ชันเปลี่ยนรหัสผ่านแทน"
    CANNOT_RESET_SUPERADMIN_PASSWORD = "ไม่สามารถรีเซ็ตรหัสผ่านของ superadmin คนอื่นได้"
    SCOPE_PASSWORD_RESET_DENIED = "ไม่มีสิทธิ์รีเซ็ตรหัสผ่านของผู้ใช้งานนี้"
    CANNOT_CREATE_SUPERADMIN = "ไม่มีสิทธิ์สร้างบัญชี superadmin"
    CANNOT_CREATE_ADMIN_OR_SUPERADMIN = "ไม่มีสิทธิ์สร้างบัญชี admin หรือ superadmin"
    PASSWORD_NOT_SET_YET = "บัญชีนี้ยังไม่ได้ตั้งรหัสผ่าน กรุณาใช้ฟังก์ชันส่งคำเชิญอีกครั้งแทน"
    ALREADY_VERIFIED = "ผู้ใช้งานนี้ยืนยันตัวตนแล้ว"
    RESEND_INVITE_COOLDOWN = "กรุณารอสักครู่ก่อนขอส่งคำเชิญอีกครั้ง"
    VILLAGE_NEEDS_ADMIN_FIRST = "หมู่บ้านนี้ยังไม่มีผู้ใช้งาน กรุณาสร้างบัญชี admin ก่อนสร้างบัญชี user"
    EMAIL_SAME_AS_CURRENT = "อีเมลใหม่ต้องไม่ซ้ำกับอีเมลเดิม"
    EMAIL_ALREADY_IN_USE = "อีเมลนี้ถูกใช้งานแล้ว"
    EMAIL_CHANGE_COOLDOWN = "กรุณารอสักครู่ก่อนขอเปลี่ยนอีเมลอีกครั้ง"

class VillageErrors:
    NOT_FOUND = "ไม่พบหมู่บ้าน"





class ContactErrors:
    NOT_FOUND = "ไม่พบข้อมูลผู้ติดต่อ"
    SCOPE_ADMIN_ONLY_USER = "เฉพาะ superadmin เท่านั้นที่สามารถจัดการผู้ติดต่อของ admin หรือ superadmin ได้"
    SCOPE_OUTSIDE_VILLAGE = "ไม่สามารถจัดการผู้ติดต่อนอกหมู่บ้านของคุณได้"
    SCOPE_NOT_OWN_CONTACT = "ไม่มีสิทธิ์จัดการผู้ติดต่อของผู้ใช้งานอื่น"
    DIRECTORY_ACCESS_DENIED = "ไม่มีสิทธิ์เข้าถึงข้อมูลของผู้ใช้งานนี้"
    CUSTOM_LABEL_REQUIRED = "ต้องระบุ custom_label เมื่อ content_type เป็น 'other'"
    CUSTOM_LABEL_NOT_ALLOWED = "ไม่สามารถระบุ custom_label ได้ ยกเว้น content_type เป็น 'other'"
    INVALID_OTHER_FORMAT = "ช่องทางอื่นๆ รับเฉพาะภาษาไทย ภาษาอังกฤษ ตัวเลข และอักขระพิเศษเท่านั้น"
    DUPLICATE_CONTENT_TYPE = "ผู้ใช้งานมีข้อมูลการติดต่อประเภทนี้อยู่แล้ว"
    INVALID_PHONE_FORMAT = "เบอร์โทรศัพท์ต้องมีความยาว 12 ตัวอักษร ประกอบด้วยตัวเลข 0-9 และเครื่องหมาย - เท่านั้น"
    INVALID_LINE_FORMAT = "Line ID ต้องมีความยาว 4-20 ตัวอักษร ใช้ได้เฉพาะตัวพิมพ์เล็ก a-z ตัวเลข 0-9 จุด ขีดกลาง และขีดล่างเท่านั้น"
    INVALID_FACEBOOK_FORMAT = "Facebook ต้องมีความยาวไม่เกิน 50 ตัวอักษร"
    INVALID_INSTAGRAM_FORMAT = "Instagram ต้องมีความยาว 1-30 ตัวอักษร ใช้ได้เฉพาะตัวอักษรภาษาอังกฤษ a-z ตัวเลข 0-9 จุด และขีดล่างเท่านั้น"

    @staticmethod
    def max_contacts_reached(limit: int) -> str:
        return f"ผู้ใช้งานมีผู้ติดต่อครบจำนวนสูงสุด {limit} รายการแล้ว"


class BlacklistErrors:
    NOT_FOUND = "ไม่พบรายการในบัญชีดำ"
    PLATE_IS_WHITELISTED = "ทะเบียนนี้อยู่ในบัญชีขาวของหมู่บ้านนี้อยู่แล้ว"


class WhitelistErrors:
    NOT_FOUND = "ไม่พบรายการในบัญชีขาว"
    PLATE_IS_BLACKLISTED = "ทะเบียนนี้อยู่ในบัญชีดำของหมู่บ้านนี้อยู่แล้ว"


class StorageErrors:
    INVALID_IMAGE = "ไฟล์รูปภาพไม่ถูกต้องหรือเสียหาย"
    INVALID_PATH = "เส้นทางไฟล์ไม่ถูกต้อง"

    @staticmethod
    def unsupported_image_content_type(content_type: str | None) -> str:
        return f"ไม่รองรับประเภทไฟล์รูปภาพ: {content_type}"

    @staticmethod
    def image_too_large(max_mb: int) -> str:
        return f"ขนาดรูปภาพเกิน {max_mb}MB ที่กำหนด"

    @staticmethod
    def unsupported_format(image_format: str | None) -> str:
        return f"ไม่รองรับรูปแบบไฟล์รูปภาพ: {image_format}"


class NotificationErrors:
    NOT_FOUND = "ไม่พบการแจ้งเตือน"


class RealtimeErrors:
    INVALID_OR_EXPIRED_TICKET = "การเชื่อมต่อหมดอายุ กรุณารีเฟรชหน้าเว็บ"
    TICKET_REQUIRED = "ต้องระบุ ticket อย่างน้อย 1 รายการ"
    TICKET_SESSION_MISMATCH = "ticket ที่ส่งมาไม่ได้มาจากเซสชันเดียวกัน"
    SESSION_REVOKED = "เซสชันนี้ถูกยกเลิกแล้ว กรุณาเข้าสู่ระบบใหม่"

    @staticmethod
    def stream_limit_reached(max_per_session: int) -> str:
        return f"หน้านี้หยุดรับการแจ้งเตือนแล้ว เนื่องจากเปิดเกิน {max_per_session} หน้าต่างบนอุปกรณ์นี้"


class ValidationErrors:
    PASSWORD_MIN_LENGTH = "รหัสผ่านต้องมีความยาวอย่างน้อย 8 ตัวอักษร"
    PASSWORD_NEED_LETTER = "รหัสผ่านต้องมีตัวอักษรอย่างน้อย 1 ตัว"
    PASSWORD_NEED_DIGIT = "รหัสผ่านต้องมีตัวเลขอย่างน้อย 1 ตัว"
    PASSWORD_MAX_LENGTH = "รหัสผ่านต้องมีความยาวไม่เกิน 36 ตัวอักษร"
    PASSWORD_NEED_SYMBOL = "รหัสผ่านต้องมีอักขระพิเศษอย่างน้อย 1 ตัว"
    PASSWORD_MISMATCH = "รหัสผ่านใหม่และรหัสผ่านยืนยันไม่ตรงกัน"
    SUPERADMIN_NO_VILLAGE = "superadmin ต้องไม่มี village_id"
    VILLAGE_REQUIRED_FOR_ROLE = "ต้องระบุ village_id สำหรับสิทธิ์การใช้งานนี้"
    PASSWORD_INVALID_CHARACTERS = "รหัสผ่านรองรับเฉพาะตัวอักษรภาษาอังกฤษ ตัวเลข และสัญลักษณ์เท่านั้น"

class DetectionErrors:
    NOT_FOUND = "ไม่พบข้อมูลการตรวจจับ"
    IMAGE_FILE_NOT_FOUND = "ไม่พบไฟล์รูปภาพ"
    CAMERA_VILLAGE_INACTIVE = "หมู่บ้านของกล้องนี้ถูกระงับการใช้งาน ไม่สามารถรับข้อมูลการตรวจจับได้"
    CAMERA_INACTIVE = "กล้องนี้ถูกปิดการใช้งาน ไม่สามารถรับข้อมูลการตรวจจับได้"
    STORE_IMAGE_FAILED = "บันทึกรูปภาพการตรวจจับไม่สำเร็จ"
    REQUIRED_FILES_MISSING = "ต้องแนบไฟล์ image_crop และ image_full"
    LICENSE_PLATE_REQUIRED = "ต้องระบุ license_plate"
    DATE_RANGE_INVALID = "date_to ต้องมากกว่าหรือเท่ากับ date_from"

    @staticmethod
    def unsupported_request_content_type(content_type: str) -> str:
        return f"ไม่รองรับ Content-Type: {content_type}"

    @staticmethod
    def date_range_too_wide(max_days: int) -> str:
        return f"ช่วงวันที่ต้องไม่เกิน {max_days} วัน"

class OnvifErrors:
    CONNECTION_FAILED = "ไม่สามารถเชื่อมต่อกล้องได้ กรุณาตรวจสอบ IP และพอร์ต"
    INVALID_CREDENTIALS = "ชื่อผู้ใช้หรือรหัสผ่านของกล้องไม่ถูกต้อง"
    NO_MEDIA_PROFILES = "ไม่พบ media profile บนกล้องนี้"
    UNSUPPORTED_OR_UNREACHABLE = "กล้องนี้ไม่รองรับ ONVIF หรือมีปัญหาในการเชื่อมต่อ"

class CameraErrors:
    NAME_ALREADY_EXISTS = "ชื่อกล้องนี้มีอยู่แล้วในหมู่บ้าน กรุณาตั้งชื่ออื่น"
    NOT_FOUND = "ไม่พบกล้อง"
    DELETE_HAS_DETECTIONS = "กล้องนี้มีข้อมูลการตรวจจับผูกอยู่ ไม่สามารถลบได้ กรุณาปิดการใช้งานแทน"
    DELETE_ALREADY_LINKED = "กล้องนี้เชื่อมต่อกับระบบ AI vision แล้ว ไม่สามารถลบได้ กรุณาปิดการใช้งานแทน"
    SYNC_WITH_AI_VISION_FAILED = "ซิงค์ข้อมูลกล้องกับระบบ AI vision ไม่สำเร็จ"
    STREAM_UNAVAILABLE_INACTIVE = "กล้องนี้ถูกปิดการใช้งาน ไม่สามารถขอลิงก์สตรีมได้"
    CANNOT_CREATE_VILLAGE_INACTIVE = "หมู่บ้านนี้ถูกปิดการใช้งาน ไม่สามารถเพิ่มกล้องได้ กรุณาเปิดใช้งานหมู่บ้านก่อน"
    CANNOT_ACTIVATE_VILLAGE_INACTIVE = "หมู่บ้านนี้ถูกปิดการใช้งาน ไม่สามารถเปิดใช้งานกล้องได้ กรุณาเปิดใช้งานหมู่บ้านก่อน"
    INVALID_RTSP_FORMAT = "รูปแบบ URL ของสตรีม (RTSP) ไม่ถูกต้อง"

class AvatarErrors:
    NOT_SET = "ผู้ใช้งานนี้ยังไม่ได้ตั้งรูปโปรไฟล์"
    ALREADY_NULL = "ผู้ใช้งานนี้ไม่มีรูปโปรไฟล์ให้ลบ"
    STORE_FAILED = "บันทึกรูปโปรไฟล์ไม่สำเร็จ"
