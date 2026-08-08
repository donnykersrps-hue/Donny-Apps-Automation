import streamlit as st
import zipfile
import xml.etree.ElementTree as ET
import pandas as pd
import io
import re
import os
import folium
from streamlit_folium import st_folium
import openpyxl
import requests

# Konfigurasi Halaman Streamlit
st.set_page_config(
    page_title="Otomatisasi Rekap Ruas Jalan KMZ",
    page_icon="🛣️",
    layout="wide"
)

def convert_dms_to_dd(dms_str):
    """Mengubah format DMS ke Decimal Degrees."""
    try:
        parts = dms_str.split(',')
        if len(parts) != 2:
            return None, None
        
        lat_part = parts[0].strip()
        lon_part = parts[1].strip()

        def parse_single_dms(s):
            match = re.search(r"(\d+)°\s*(\d+)['\']\s*([\d.]+)\"\s*([NSEWnsew])", s)
            if not match:
                return None
            deg, m, sec, direction = match.groups()
            dd = float(deg) + float(m)/60 + float(sec)/3600
            if direction.upper() in ['S', 'W']:
                dd = -dd
            return dd

        lat = parse_single_dms(lat_part)
        lon = parse_single_dms(lon_part)
        return lat, lon
    except Exception:
        return None, None

def format_dd_to_dms(lat, lon):
    """Mengubah desimal latitude/longitude kembali ke format DMS standar."""
    def dd_to_dms_single(val, is_lat):
        direction = 'S' if is_lat and val < 0 else ('N' if is_lat else ('W' if val < 0 else 'E'))
        val = abs(val)
        degrees = int(val)
        minutes_float = (val - degrees) * 60
        minutes = int(minutes_float)
        seconds = round((minutes_float - minutes) * 60, 2)
        return f"{degrees}°{minutes}'{seconds}\"{direction}"
    
    return f"{dd_to_dms_single(lat, True)}, {dd_to_dms_single(lon, False)}"

def extract_kml_from_kmz_bytes(kmz_bytes):
    import tempfile
    temp_dir = tempfile.mkdtemp()
    kmz_path = os.path.join(temp_dir, "temp.kmz")
    with open(kmz_path, "wb") as f:
        f.write(kmz_bytes)
    
    with zipfile.ZipFile(kmz_path, 'r') as zip_ref:
        zip_ref.extractall(temp_dir)
        
    kml_path = os.path.join(temp_dir, "doc.kml")
    return kml_path, temp_dir

def parse_kml_all_linestrings(kml_path):
    tree = ET.parse(kml_path)
    root = tree.getroot()
    ns = {'kml': 'http://www.opengis.net/kml/2.2'}
    
    coordinates_points = []
    for coord_elem in root.findall('.//kml:LineString/kml:coordinates', ns):
        text = coord_elem.text
        if text:
            raw_coords = text.strip().split()
            for item in raw_coords:
                parts = item.split(',')
                if len(parts) >= 2:
                    lon = float(parts[0])
                    lat = float(parts[1])
                    coordinates_points.append((lon, lat))
    return coordinates_points

def reverse_geocode_photon(lat, lon):
    try:
        url = f"https://photon.komoot.io/reverse?lon={lon}&lat={lat}"
        headers = {'User-Agent': 'KMZRoadAutomationApp/1.0'}
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            data = res.json()
            if data and 'features' in data and len(data['features']) > 0:
                props = data['features'][0]['properties']
                road_name = props.get('name', '')
                district = props.get('district', props.get('suburb', ''))
                city = props.get('city', props.get('county', ''))
                state = props.get('state', '')
                return road_name, district, city, state
    except Exception:
        pass
    return "", "", "", ""

def process_single_kmz(kmz_bytes, spk, ring_id, area_name, status_text):
    kml_path, temp_dir = extract_kml_from_kmz_bytes(kmz_bytes)
    points = parse_kml_all_linestrings(kml_path)
    
    import shutil
    shutil.rmtree(temp_dir, ignore_errors=True)
    
    if not points:
        return []
    
    from math import radians, cos, sin, asin, sqrt
    def haversine(lon1, lat1, lon2, lat2):
        lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])
        dlon = lon2 - lon1 
        dlat = lat2 - lat1 
        a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
        c = 2 * asin(sqrt(a)) 
        r = 6371000 
        return c * r

    total_len = 0.0
    for i in range(len(points) - 1):
        total_len += haversine(points[i][0], points[i][1], points[i+1][0], points[i+1][1])
        
    start_pt = points[0]
    end_pt = points[-1]
    
    start_dms = format_dd_to_dms(start_pt[1], start_pt[0])
    end_dms = format_dd_to_dms(end_pt[1], end_pt[0])
    
    mid_idx = len(points) // 2
    road_name, district, city, state = reverse_geocode_photon(points[mid_idx][1], points[mid_idx][0])
    if not road_name:
        road_name = f"Ruas Jalan {ring_id}"

    dest = area_name if area_name else "Nasional"
    area = f"{district}, {city}".strip(", ") if (district or city) else "Sektor Wilayah"
    
    segment = {
        "SPK": spk,
        "Ring ID": ring_id,
        "Destination": dest,
        "Area": area,
        "Authority Ruas Jalan": "Non Status",
        "Instansi": "Pemerintah Daerah",
        "Nama Ruas Jalan Implementasi": road_name,
        "Panjang Ruas Jalan (Meter)": round(total_len, 2),
        "Status Cable": "New Cable",
        "Titik Koordinat Awal": start_dms,
        "Titik Koordinat Akhir": end_dms,
        "Status Survey": "Done Survey"
    }
    
    return [segment]

# Tampilan Antarmuka Streamlit
st.title("🛣️ Otomatisasi Rekap Ruas Jalan KMZ")
st.write("Ekstraksi data ruas jalan dari file KMZ menjadi Laporan Excel Master 12 Kolom.")

uploaded_files = st.file_uploader("Unggah File KMZ (Bisa Pilih Banyak File)", type=["kmz"], accept_multiple_files=True)

if uploaded_files:
    st.info(f"Total file terpilih: **{len(uploaded_files)} file KMZ**")
    
    if st.button("⚡ Proses Semua File & Buat Excel Master", type="primary"):
        progress_bar = st.progress(0)
        status_text = st.empty()
        all_master_data = []

        for index, uploaded_file in enumerate(uploaded_files, 1):
            basename = os.path.splitext(uploaded_file.name)[0]
            if "-" in basename:
                parts = basename.split("-", 1)
                full_code = parts[0].strip()
                area_name = parts[1].strip()
            else:
                full_code = basename.strip()
                area_name = ""

            if len(full_code) >= 4 and full_code[:4].isdigit():
                spk = full_code[:4]
                ring_id = full_code[4:]
            else:
                spk = full_code[:5]
                ring_id = full_code

            progress_bar.progress((index - 1) / len(uploaded_files))
            status_text.text(f"[{index}/{len(uploaded_files)}] Memproses {spk} - {ring_id}...")

            try:
                kmz_bytes = uploaded_file.read()
                segments = process_single_kmz(kmz_bytes, spk, ring_id, area_name, status_text)
                all_master_data.extend(segments)
            except Exception as e:
                st.error(f"Gagal memproses file {uploaded_file.name}: {e}")

        progress_bar.progress(1.0)

        if all_master_data:
            columns = [
                "SPK", "Ring ID", "Destination", "Area", 
                "Authority Ruas Jalan", "Instansi", "Nama Ruas Jalan Implementasi", 
                "Panjang Ruas Jalan (Meter)", "Status Cable", 
                "Titik Koordinat Awal", "Titik Koordinat Akhir", "Status Survey"
            ]
            df_master = pd.DataFrame(all_master_data, columns=columns)
            st.session_state["df_master"] = df_master
            st.session_state["processed"] = True

# Tampilkan Hasil Pemrosesan jika data tersimpan di session_state
if st.session_state.get("processed", False):
    st.success("✅ Selesai! File Master Excel berhasil dibuat.")

    # Export ke Excel via Memory Buffer
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        st.session_state["df_master"].to_excel(writer, index=False, sheet_name='Master Data')
        ws = writer.sheets['Master Data']
        ws.views.sheetView[0].showGridLines = True
        
        hdr_fill = openpyxl.styles.PatternFill(start_color="003366", end_color="003366", fill_type="solid")
        hdr_font = openpyxl.styles.Font(name="Arial", size=10, bold=True, color="FFFFFF")
        border = openpyxl.styles.Border(
            left=openpyxl.styles.Side(style='thin', color='D9D9D9'),
            right=openpyxl.styles.Side(style='thin', color='D9D9D9'),
            top=openpyxl.styles.Side(style='thin', color='D9D9D9'),
            bottom=openpyxl.styles.Side(style='thin', color='D9D9D9')
        )
        
        for col_num in range(1, 13):
            cell = ws.cell(row=1, column=col_num)
            cell.fill, cell.font, cell.border = hdr_fill, hdr_font, border
            cell.alignment = openpyxl.styles.Alignment(horizontal="center", vertical="center")
            
        for row in range(2, len(st.session_state["df_master"]) + 2):
            for col in range(1, 13):
                cell = ws.cell(row=row, column=col)
                cell.border = border
                if col == 8:
                    cell.number_format = '#,##0.00'
                    cell.alignment = openpyxl.styles.Alignment(horizontal="right")
                elif col in [1, 2, 5, 9, 10, 11, 12]:
                    cell.alignment = openpyxl.styles.Alignment(horizontal="center")
                else:
                    cell.alignment = openpyxl.styles.Alignment(horizontal="left")
                    
        widths = {'A':10, 'B':16, 'C':15, 'D':22, 'E':20, 'F':25, 'G':35, 'H':25, 'I':15, 'J':30, 'K':30, 'L':18}
        for col_letter, width in widths.items():
            ws.column_dimensions[col_letter].width = width

    output.seek(0)

    st.download_button(
        label="📥 Unduh File Excel Master",
        data=output.getvalue(),
        file_name=f"Rekap_Master_Ruas_Jalan_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary"
    )

    # PREVIEW TABEL CENTANG MULTI-ROW
    st.subheader("📊 Preview Tabel Master Excel (Centang baris untuk menampilkan jalur di peta)")
    
    event = st.dataframe(
        st.session_state["df_master"],
        on_select="rerun",
        selection_mode="multi-row",
        use_container_width=True
    )

    # PREVIEW PETA INTERAKTIF DENGAN POPUP KEMBALI
    st.subheader("🗺️ Preview Peta Ruas Jalan Interaktif")
    try:
        uploaded_files[0].seek(0)
        kml_p, temp_d = extract_kml_from_kmz_bytes(uploaded_files[0].read())
        map_points = parse_kml_all_linestrings(kml_p)
        import shutil
        shutil.rmtree(temp_d, ignore_errors=True)

        if map_points and len(map_points) > 0:
            selected_rows = event.selection.get("rows", [])
            total_seg = len(st.session_state["df_master"])
            pts_per_seg = max(1, len(map_points) // total_seg)

            center_lat = map_points[len(map_points)//2][1]
            center_lon = map_points[len(map_points)//2][0]
            
            m = folium.Map(location=[center_lat, center_lon], zoom_start=15, tiles=None)
            
            # Layer Peta Pilihan
            folium.TileLayer('OpenStreetMap', name='Peta Jalan Standar').add_to(m)
            folium.TileLayer(
                tiles='https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}',
                attr='Google Hybrid',
                name='Satelit + Teks Nama Jalan (Google Earth)'
            ).add_to(m)

            if selected_rows:
                selected_names = []
                last_center_lat, last_center_lon = center_lat, center_lon
                
                for r_idx in selected_rows:
                    row_data = st.session_state["df_master"].iloc[r_idx]
                    road_name = row_data["Nama Ruas Jalan Implementasi"]
                    road_len = row_data["Panjang Ruas Jalan (Meter)"]
                    selected_names.append(road_name)
                    
                    idx_a = min(r_idx * pts_per_seg, len(map_points) - 1)
                    idx_b = min((r_idx + 1) * pts_per_seg, len(map_points) - 1)
                    if idx_a == idx_b and idx_b < len(map_points) - 1:
                        idx_b += 1
                        
                    seg_points = map_points[idx_a : idx_b + 1]
                    line_coords = [[pt[1], pt[0]] for pt in seg_points]
                    
                    # Garis Ungu DENGAN POPUP POPUP TEKS / TOOLTIP PERMANEN
                    folium.PolyLine(
                        line_coords, 
                        color="#6c5ce7", 
                        weight=7, 
                        opacity=0.9, 
                        popup=folium.Popup(f"<b>Ruas Jalan:</b> {road_name}<br><b>Panjang:</b> {road_len} m", max_width=300),
                        tooltip=folium.Tooltip(f"<b>{road_name}</b> ({road_len} m)", permanent=True)
                    ).add_to(m)
                    
                    # Pin Titik Awal & Akhir
                    s_lat, s_lon = seg_points[0][1], seg_points[0][0]
                    e_lat, e_lon = seg_points[-1][1], seg_points[-1][0]
                    
                    folium.Marker(
                        location=[s_lat, s_lon],
                        popup=f"<b>TITIK A (AWAL)</b><br>{road_name}",
                        tooltip=f"Start: {road_name}",
                        icon=folium.Icon(color="green", icon="play")
                    ).add_to(m)
                    
                    folium.Marker(
                        location=[e_lat, e_lon],
                        popup=f"<b>TITIK B (AKHIR)</b><br>{road_name}",
                        tooltip=f"End: {road_name}",
                        icon=folium.Icon(color="red", icon="flag")
                    ).add_to(m)
                    
                    last_center_lat = (s_lat + e_lat) / 2
                    last_center_lon = (s_lon + e_lon) / 2
                
                m.location = [last_center_lat, last_center_lon]
                st.info(f"📍 **Ruas Terpilih ({len(selected_rows)}):** {', '.join(selected_names)}")
            else:
                st.caption("💡 *Centang baris pada tabel di atas untuk memunculkan garis jalur ruas jalan di peta.*")

            folium.LayerControl(position='topright').add_to(m)
            st_folium(m, use_container_width=True, height=520)
    except Exception as e:
        st.warning(f"Gagal memuat preview peta: {e}")

# --- KUSTOMISASI TAMPILAN CSS ---
st.markdown("""
    <style>
    div[data-testid="stFileUploader"] section button {
        background-color: #6c5ce7 !important;
        color: white !important;
        border: none !important;
        border-radius: 8px !important;
        font-weight: bold !important;
    }
    div[data-testid="stFileUploader"] section button:hover {
        background-color: #5a4bcf !important;
        color: white !important;
    }
    </style>
""", unsafe_allow_html=True)
