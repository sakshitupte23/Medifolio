from flask import Flask, render_template, request, jsonify
import json
import os
import threading
import time
import uuid
from datetime import datetime
from werkzeug.utils import secure_filename

# ------------------Firebase setup---------------

import firebase_admin
from firebase_admin import credentials, firestore

# Initialize Firebase Firestore
cred = credentials.Certificate("medifolio-firebase-adminsdk.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

# Collections
users_ref = db.collection("users")
admins_ref = db.collection("admins")


# ------------------- Fingerprint Sensor -------------------
try:
    from pyfingerprint.pyfingerprint import PyFingerprint
except:
    PyFingerprint = None

# Initialize sensor
finger = None
if PyFingerprint:
    try:
        finger = PyFingerprint('/dev/serial0', 57600, 0xFFFFFFFF, 0x00000000)
        finger.verifyPassword()
        print("Sensor Connected!")
    except Exception as e:
        print("Sensor init error:", e)
        finger = None
else:
    print("pyfingerprint not installed!")

UPLOAD_FOLDER = "static/uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)



lock = threading.Lock()
task_status = {}

# ------------------- Flask App -------------------
app = Flask(__name__)


@app.route("/")
def home():
    return render_template("index.html")

@app.route("/admin-login")
def admin_login():
    return render_template("admin-login.html")

@app.route("/add-patient")
def add_patient():
    return render_template("add-patient.html")

@app.route("/patient-data")
def patient_data():
    return render_template("patient-data.html")

@app.route("/search-blood")
def search_blood():
    return render_template("search-blood.html")

@app.route("/hidden/add-admin")
def add_admin_get():
    return render_template("add-admin.html")

@app.route("/hidden/list_admins")
def list_admins():
    admins = [doc.to_dict() for doc in admins_ref.stream()]
    return jsonify(admins)

# ------------------- START ADMIN SCAN -------------------
@app.route("/hidden/add_admin", methods=["POST"])
def add_admin():
    data = request.get_json()
    name = data.get("name")

    if not name:
        return jsonify({"error": "Name required"}), 400

    task_id = str(uuid.uuid4())
    threading.Thread(target=scan_and_save_admin, args=(name, task_id), daemon=True).start()

    return jsonify({"task_id": task_id})

@app.route("/hidden/admin/delete_all_data", methods=["DELETE"])
def delete_all_data():
    """
    Deletes:
    - All Firestore users
    - All Firestore admins
    - All fingerprint templates from sensor
    """
    try:
        # ---- Delete all users ----
        users = users_ref.stream()
        deleted_users = 0
        for doc in users:
            users_ref.document(doc.id).delete()
            deleted_users += 1

        # ---- Delete all admins ----
        admins = admins_ref.stream()
        deleted_admins = 0
        for doc in admins:
            admins_ref.document(doc.id).delete()
            deleted_admins += 1

        # ---- Delete fingerprint templates ----
        sensor_templates = None
        if finger:
            sensor_templates = finger.getTemplateCount()
            finger.clearDatabase()  # deletes ALL stored fingerprint templates

        return jsonify({
            "message": "All data cleared successfully",
            "deleted_users": deleted_users,
            "deleted_admins": deleted_admins,
            "sensor_templates_cleared": sensor_templates
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route("/add_user", methods=["POST"])
def add_user():
    form = request.form
    files = request.files

    # ----------- REQUIRED MINIMAL FIELDS (YOU CAN REMOVE IF NOT NEEDED) ----------- 
    full_name = form.get("full_name")
    if not full_name:
        return jsonify({"error": "full_name required"}), 400

    # ----------- SAVE PHOTO -----------
    photo = files.get("photo")
    photo_path = save_file(photo)

    # ----------- SAVE MULTIPLE DOCUMENTS -----------
    documents_paths = []
    if "documents[]" in files:
        docs = request.files.getlist("documents[]")
        for d in docs:
            documents_paths.append(save_file(d))

    # ----------- COLLECT ALL FORM FIELDS INTO JSON -----------

    user_data = {
        # --- Patient identity ---
        "full_name": full_name,
        "dob": form.get("dob"),
        "blood_group": form.get("blood_group"),
        "body_marks": form.get("body_marks"),

        # --- Contact info ---
        "address": form.get("address"),
        "contact": form.get("contact"),
        "emergency_contact": form.get("emergency_contact"),

        # --- Alerts ---
        "allergies": form.get("allergies"),
        "medical_alerts": form.get("medical_alerts"),

        # --- Medical history ---
        "medical_history": form.get("medical_history"),
        "medications": form.get("medications"),
        "family_history": form.get("family_history"),
        "lifestyle": form.get("lifestyle"),
        "physical_exam": form.get("physical_exam"),
        "additional_notes": form.get("additional_notes"),

        # --- File uploads ---
        "photo": photo_path,
        "documents": documents_paths,

        # --- Provider info ---
        "provider_name": form.get("provider_name"),
        "provider_contact": form.get("provider_contact"),
        "provider_affiliation": form.get("provider_affiliation"),
        "last_update": form.get("last_update"),
    }

    # UNIQUE TRACKING ID
    task_id = str(uuid.uuid4())

    # LAUNCH BACKGROUND THREAD
    threading.Thread(
        target=scan_and_save,
        args=(user_data, task_id),
        daemon=True
    ).start()

    return jsonify({"task_id": task_id})

#-----------------update user---------------

@app.route("/update_user/<user_id>", methods=["POST"])
def update_user(user_id):
    try:
        form = request.form
        files = request.files

        # Get existing record first
        existing = users_ref.document(user_id).get()
        if not existing.exists:
            return jsonify({"error": "User not found"}), 404

        existing_data = existing.to_dict()
        update_data = {}

        # -------------------------
        # 1. TEXT FIELDS (same as add_user)
        # -------------------------
        fields = [
            "full_name", "dob", "blood_group", "body_marks",
            "address", "contact", "emergency_contact",
            "allergies", "medical_alerts",
            "medical_history", "medications", "family_history",
            "lifestyle", "physical_exam", "additional_notes",
            "provider_name", "provider_contact", "provider_affiliation",
            "last_update"
        ]

        for field in fields:
            value = form.get(field)
            if value is not None:
                update_data[field] = value

        # -------------------------
        # 2. NEW PHOTO (optional)
        # -------------------------
        if "photo" in files and files["photo"].filename != "":
            update_data["photo"] = save_file(files["photo"])
        else:
            # keep old photo
            update_data["photo"] = existing_data.get("photo")

        # -------------------------
        # 3. NEW DOCUMENTS (optional)
        # -------------------------
        documents_paths = []

        if "documents[]" in files:
            new_docs = request.files.getlist("documents[]")
            for d in new_docs:
                if d and d.filename != "":
                    documents_paths.append(save_file(d))

        if documents_paths:
            # replace with new
            update_data["documents"] = documents_paths
        else:
            # keep old
            update_data["documents"] = existing_data.get("documents", [])

        # -------------------------
        # 4. SAVE TO FIRESTORE
        # -------------------------
        users_ref.document(user_id).update(update_data)

        return jsonify({"success": True, "updated": update_data})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500



@app.route("/validate_user")
def validate_user():
    task_id = str(uuid.uuid4())
    threading.Thread(target=scan_and_validate, args=(task_id,), daemon=True).start()
    return jsonify({"task_id": task_id})

@app.route("/validate_admin")
def validate_admin():
    task_id = str(uuid.uuid4())
    threading.Thread(target=scan_and_validate_admin, args=(task_id,), daemon=True).start()
    return jsonify({"task_id": task_id})


@app.route("/task/<task_id>")
def get_task(task_id):
    return jsonify(task_status.get(task_id, {"status": "unknown"}))


@app.route("/list_users")
def list_users():
    users = [doc.to_dict() for doc in users_ref.stream()]
    return jsonify(users)


def save_file(file_storage):
    """Save a single file and return its relative path."""
    if not file_storage:
        return None
    filename = secure_filename(file_storage.filename)
    file_id = str(uuid.uuid4()) + "_" + filename
    path = os.path.join(UPLOAD_FOLDER, file_id)
    file_storage.save(path)
    return path  


# ------------------- Fingerprint Add Logic -------------------
def scan_and_save(user_data, task_id):
    task_status[task_id] = {"status": "place_finger_1"}

    if finger is None:
        task_status[task_id] = {"status": "error", "message": "Sensor not connected"}
        return

    try:
        # 1st scan
        while not finger.readImage():
            time.sleep(0.1)

        finger.convertImage(0x01)

        res = finger.searchTemplate()
        if res[0] >= 0:
            task_status[task_id] = {"status": "exists", "position": res[0]}
            return

        # remove finger
        task_status[task_id] = {"status": "remove_finger"}
        while finger.readImage():
            time.sleep(1)
            
            

        # 2nd scan
        task_status[task_id] = {"status": "place_finger_2"}
        while not finger.readImage():
            time.sleep(0.1)

        finger.convertImage(0x02)

        if finger.compareCharacteristics() == 0:
            task_status[task_id] = {"status": "mismatch"}
            return

        finger.createTemplate()
        pos = finger.storeTemplate()

        # ------- Build final user JSON -------
        user = {
            "id": str(uuid.uuid4()),
            "template_position": pos,
            "created_at": datetime.utcnow().isoformat(),

            # Attach ALL patient fields
            **user_data
        }

        # ------- Save user to DB -------
        users_ref.document(user["id"]).set(user)

        # ------- Final success status -------
        task_status[task_id] = {"status": "done", "position": pos, "user": user}

    except Exception as e:
        task_status[task_id] = {"status": "error", "message": str(e)}

# ------------------- Fingerprint Validation Logic -------------------
def scan_and_validate(task_id):
    task_status[task_id] = {"status": "place_finger"}

    if finger is None:
        task_status[task_id] = {"status": "error", "message": "Sensor not connected"}
        return

    try:
        while not finger.readImage():
            time.sleep(0.1)

        finger.convertImage(0x01)
        result = finger.searchTemplate()

        position = result[0]
        accuracy = result[1]

        if position == -1:
            task_status[task_id] = {"status": "nomatch"}
            return

        # find user by template position
        query = users_ref.where("template_position", "==", position).stream()
        matched_user = None
        for doc in query:
            matched_user = doc.to_dict()
            break


        task_status[task_id] = {
            "status": "matched",
            "position": position,
            "accuracy": accuracy,
            "user": matched_user
        }

    except Exception as e:
        task_status[task_id] = {"status": "error", "message": str(e)}


# ------------------- Fingerprint Admin Login Logic -------------------
def scan_and_validate_admin(task_id):
    task_status[task_id] = {"status": "place_finger"}

    if finger is None:
        task_status[task_id] = {"status": "error", "message": "Sensor not connected"}
        return

    try:
        while not finger.readImage():
            time.sleep(0.1)

        finger.convertImage(0x01)
        result = finger.searchTemplate()

        position = result[0]
        accuracy = result[1]

        if position == -1:
            task_status[task_id] = {"status": "nomatch"}
            return

        # find user by template position
        query = admins_ref.where("template_position", "==", position).stream()
        matched_admin = None
        for doc in query:
            matched_admin = doc.to_dict()
            break
            
        if matched_admin:
            task_status[task_id] = {
                "status": "matched",
                "position": position,
                "accuracy": accuracy,
                "user": matched_admin
            }
            return
        else:
            task_status[task_id] = {"status": "unauthorized"}
            return

    except Exception as e:
        task_status[task_id] = {"status": "error", "message": str(e)}

# ------------------- FINGERPRINT ENROLLMENT LOGIC -------------------
def scan_and_save_admin(name, task_id):
    task_status[task_id] = {"status": "place_finger_1"}

    if finger is None:
        task_status[task_id] = {"status": "error", "message": "Fingerprint sensor not connected"}
        return

    try:
        # FIRST SCAN
        while not finger.readImage():
            time.sleep(0.1)
        finger.convertImage(0x01)

        result = finger.searchTemplate()
        if result[0] >= 0:
            task_status[task_id] = {"status": "exists", "position": result[0]}
            return

        task_status[task_id] = {"status": "remove_finger"}
        while finger.readImage():
            time.sleep(1)

        # SECOND SCAN
        task_status[task_id] = {"status": "place_finger_2"}
        while not finger.readImage():
            time.sleep(0.1)
        finger.convertImage(0x02)

        if finger.compareCharacteristics() == 0:
            task_status[task_id] = {"status": "mismatch"}
            return

        finger.createTemplate()
        pos = finger.storeTemplate()

        # Save admin
        admin = {
            "id": str(uuid.uuid4()),
            "name": name,
            "template_position": pos,
            "is_admin": True,
            "created_at": datetime.utcnow().isoformat()
        }

        with lock:
            admins_ref.document(admin["id"]).set(admin)

        task_status[task_id] = {"status": "done", "position": pos, "admin": admin}

    except Exception as e:
        task_status[task_id] = {"status": "error", "message": str(e)}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)

