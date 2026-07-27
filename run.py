"""Start the drive-station app.

Simulator mode by default; set DRIVESTATION_MODE=real on the station mini PC.
"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("drivestation.web.app:app", host="0.0.0.0", port=8330)
