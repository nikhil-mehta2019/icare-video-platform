import sqlite3

conn = sqlite3.connect("icare.db")
cursor = conn.cursor()

cursor.execute("SELECT * FROM videos")
rows = cursor.fetchall()

print(rows)