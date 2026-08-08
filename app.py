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
import math
import shutil
import tempfile
from shapely.geometry import LineString
from pyproj import Transformer

# Konfigurasi Halaman Streamlit
st.set_page_config(
    page_title="Otomatisasi Rekap Ruas Jalan KMZ",
    page_icon="🛣️",
    layout="wide"
)

_api_cache = {}

def decimal_to_dms(lat, lon):
    """Konversi koordinat desimal ke format DMS presisi."""
    if lat is None or lon is None or pd.isna(lat) or pd.isna(lon): return ""
    lat_dir, lat_abs = ('N', abs(lat)) if lat >= 0 else ('S', abs(lat))
    lat_d = int(lat_abs)
    lat_m = int((lat_abs - lat_d) * 60)
    lat_s = round((lat_abs - lat_d - lat_m/60) * 3600, 2)
    if lat_s >= 60.0: lat_s, lat_m = lat_s - 60.0, lat_m + 1
    if lat_m >= 60: lat_m, lat_d = lat_m - 60, lat_d + 1
        
    lon_dir, lon_abs = ('E', abs(lon)) if lon >= 0 else ('W', abs(lon))
    lon_d = int(lon_abs)
    lon_m = int((lon_abs - lon_d) * 60)
    lon_s = round((lon_abs - lon_d - lon_m/60) * 3600, 2)
    if lon_s >= 60.0: lon_s, lon_m = lon_s - 60.0, lon_m + 1
    if lon_m >= 60: lon_m, lon_d = lon_m - 60, lon_d + 1
        
    return f'{lat_d}°{lat_m}\'{lat_s}"{lat_dir}, {lon_d}°{lon_m}\'{lon_s}"{lon_dir}'

def get_utm_epsg(lon, lat):
    zone_number = int((lon + 180) / 6) + 1
    return f"EPSG:326{zone_number:02d}" if lat >= 0 else f"EPSG:327{zone_number:02d}"

def calculate_deflection_angle(p1, p2, p3):
    v1_x, v1_y = p2[0] - p1[0], p2[1] - p1[1]
    v2_x, v2_y = p3[0] - p2[0], p3[1] - p2[1]
    b1 = math.degrees(math.atan2(v1_y, v1_x))
    b2 = math.degrees(math.atan2(v2_y, v2_x))
    diff = abs(b1 - b2)
    return 360 - diff if diff > 180 else diff

def extract_kml_from_kmz_bytes(kmz_bytes):
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
    all_coords = []
    clean_tag = lambda tag: tag.split('}')[-1] if '}' in tag else tag
    for elem in tree.getroot().iter():
        if clean_tag(elem.tag) == 'LineString':
            for child in elem.iter():
                if clean_tag(child.tag) == 'coordinates' and child.text:
                    for pt in child.text.strip().replace('\n', ' ').split():
                        parts = pt.split(',')
                        if len(parts) >= 2:
                            try:
                                lon, lat = float(parts[0]), float(parts[1])
                                if not all_coords or all_coords[-1] != (lon, lat):
                                    all_coords.append((lon, lat))
                            except ValueError: continue
                    break
    return all_coords

def get_accurate_road_info(lat, lon):
    coord_key = (round(lat, 5), round(lon, 5))
    if coord_key in _api_cache: return _api_cache[coord_key]

    road_name = "Jalan Belum Terdata (OSM)"
    authority = "Kota"
    instansi = "Dinas PUPR Kota/Kabupaten"

    try:
        url = f"https://photon.komoot.io/reverse?lon={lon}&lat={lat}"
        headers = {'User-Agent': 'KMZRoadAutomationApp/1.0'}
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            data = res.json()
            if data and 'features' in data and len(data['features']) > 0:
                props = data['features'][0].get('properties', {})
                highway_type = props.get('highway', '')
                
                road = props.get('street') or props.get('name') or props.get('highway')
                if road: road_name = str(road).title()
                else:
                    if props.get('village'): road_name = f"Jalan Akses {str(props.get('village')).title()}"
                    elif props.get('county'): road_name = f"Kawasan {str(props.get('county')).title()}"
                
                name_lower = road_name.lower()
                
                if any(kw in name_lower for kw in ['lintas', 'nasional', 'raya ']) or highway_type in ['trunk', 'primary', 'motorway']:
                    authority, instansi = "Provinsi", "Dinas PUPR Provinsi"
                elif "belum terdata" in name_lower or "akses" in name_lower or "gang " in name_lower or highway_type in ['track', 'path', 'unclassified']:
                    authority, instansi = "Desa", "Pemerintah Desa"
                else:
                    authority, instansi = "Kota", "Dinas PUPR Kota/Kabupaten"
            
            result = (road_name, authority, instansi)
            _api_cache[coord_key] = result
            return result
    except Exception: 
        return (road_name, authority, instansi)

def process_single_kmz(kmz_bytes, spk, ring_id, area_name):
    kml_path, temp_dir = extract_kml_from_kmz_bytes(kmz_bytes)
    try:
        points = parse_kml_all_linestrings(kml_path)
        if len(points) < 2: return [], []

        transformer = Transformer.from_crs("EPSG:4326", get_utm_epsg(points[0][0], points[0][1]), always_xy=True)
        segments_data = []
        seg_points_list = []
        seg_start_idx, last_api_idx = 0, 0
        
        current_road, current_auth, current_instansi = get_accurate_road_info(points[0][1], points[0][0])
        
        i = 1
        while i < len(points):
            lon, lat = points[i]
            cur_utm = transformer.transform(lon, lat)
            last_utm = transformer.transform(points[last_api_idx][0], points[last_api_idx][1])
            
            is_sharp = calculate_deflection_angle(points[i-1], points[i], points[i+1]) > 30 if i < len(points)-1 else False
            dist = math.hypot(cur_utm[0] - last_utm[0], cur_utm[1] - last_utm[1])
            
            if (i == len(points) - 1) or is_sharp or dist >= 50:
                chk_road, chk_auth, chk_inst = get_accurate_road_info(lat, lon)
                
                if chk_road and chk_road != current_road:
                    cut_idx = i
                    for j in range(last_api_idx + 1, i):
                        m_road, m_auth, m_inst = get_accurate_road_info(points[j][1], points[j][0])
                        if m_road and m_road != current_road:
                            cut_idx, chk_road, chk_auth, chk_inst = j, m_road, m_auth, m_inst
                            break
                    
                    seg_pts = points[seg_start_idx : cut_idx + 1]
                    if len(seg_pts) > 1:
                        line_utm = LineString([transformer.transform(pt[0], pt[1]) for pt in seg_pts])
                        segments_data.append({
                            "SPK": spk, "Ring ID": ring_id, "Destination": area_name if area_name else "Nasional",
                            "Area": "Sektor Wilayah", "Authority Ruas Jalan": current_auth, "Instansi": current_instansi,
                            "Nama Ruas Jalan Implementasi": current_road, "Panjang Ruas Jalan (Meter)": round(line_utm.length, 2),
                            "Status Cable": "New Cable", "Titik Koordinat Awal": decimal_to_dms(seg_pts[0][1], seg_pts[0][0]),
                            "Titik Koordinat Akhir": decimal_to_dms(seg_pts[-1][1], seg_pts[-1][0]), "Status Survey": "Done Survey"
                        })
                        seg_points_list.append(seg_pts)
                    
                    current_road, current_auth, current_instansi = chk_road, chk_auth, chk_inst
                    seg_start_idx = cut_idx
                    last_api_idx = cut_idx
                    i = cut_idx + 1
                    continue
                last_api_idx = i
            i += 1

        if seg_start_idx < len(points) - 1:
            seg_pts = points[seg_start_idx : len(points)]
            if len(seg_pts) > 1:
                line_utm = LineString([transformer.transform(pt[0], pt[1]) for pt in seg_pts])
                segments_data.append({
                    "SPK": spk, "Ring ID": ring_id, "Destination": area_name if area_name else "Nasional",
                    "Area": "Sektor Wilayah", "Authority Ruas Jalan": current_auth, "Instansi": current_instansi,
                    "Nama Ruas Jalan Implementasi": current_road, "Panjang Ruas Jalan (Meter)": round(line_utm.length, 2),
                    "Status Cable": "New Cable", "Titik Koordinat Awal": decimal_to_dms(seg_pts[0][1], seg_pts[0][0]),
                    "Titik Koordinat Akhir": decimal_to_dms(seg_pts[-1][1], seg_pts[-1][0]), "Status Survey": "Done Survey"
                })
                seg_points_list.append(seg_pts)

        return segments_data, seg_points_list
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

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
        all_map_lines = []

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
            status_text.text(f"[{index}/{len(uploaded_files)}] Memindai ruas jalan {spk} - {ring_id}...")

            try:
                kmz_bytes = uploaded_file.read()
                segments, seg_points_list = process_single_kmz(kmz_bytes, spk, ring_id, area_name)
                all_master_data.extend(segments)
                all_map_lines.extend(seg_points_list)
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
            # Tambahkan kolom centang khusus untuk Filter Peta
            if "Pilih Peta" not in df_master.columns:
                df_master.insert(0, "Pilih Peta", True)
                
            st.session_state["df_master"] = df_master
            st.session_state["all_map_lines"] = all_map_lines
            st.session_state["processed"] = True

# Tampilkan Hasil Pemrosesan jika data tersimpan di session_state
if st.session_state.get("processed", False) and "df_master" in st.session_state:
    st.success("✅ Selesai! File Master Excel berhasil dibuat.")

    # PREVIEW & EDIT TABEL MASTER EXCEL
    st.subheader("📊 Preview & Edit Tabel Master Excel")
    st.caption("💡 *Kak Donny bisa meng-klik 2x sel mana saja untuk edit data, dan centang/hilangkan centang pada kolom 'Pilih Peta' untuk memfilter peta.*")

    # Pastikan kolom Pilih Peta ada
    if "Pilih Peta" not in st.session_state["df_master"].columns:
        st.session_state["df_master"].insert(0, "Pilih Peta", True)

    # TABEL EDITABLE + FITUR CENTANG PETA PADA KOLOM PERTAMA
    edited_df = st.data_editor(
        st.session_state["df_master"],
        use_container_width=True,
        column_config={
            "Pilih Peta": st.column_config.CheckboxColumn(
                "Pilih Peta",
                help="Centang untuk menampilkan garis ruas jalan ini di peta",
                default=True,
            )
        },
        key="master_editor"
    )

    # Simpan data editan terbaru Kak Donny ke session state
    st.session_state["df_master"] = edited_df

    # Dataframe khusus untuk di-export ke Excel (Tanpa Kolom "Pilih Peta")
    df_for_excel = edited_df.drop(columns=["Pilih Peta"], errors="ignore")

    # Export ke Excel via Memory Buffer dari Data Master
    output_edited = io.BytesIO()
    with pd.ExcelWriter(output_edited, engine='openpyxl') as writer:
        df_for_excel.to_excel(writer, index=False, sheet_name='Master Data')
        ws = writer.sheets['Master Data']
        ws.views.sheetView[0].showGridLines = True
        
        hdr_fill = openpyxl.styles.PatternFill(start_color="003366", end_color="003366", fill_type="solid")
        hdr_font = openpyxl.styles.Font(name="Arial", size=10, bold=True, color="FFFFFF")
        border = openpyxl.styles.Border(
            left=openpyxl.styles.Side(style='thin', color='D9D9D9'), right=openpyxl.styles.Side(style='thin', color='D9D9D9'),
            top=openpyxl.styles.Side(style='thin', color='D9D9D9'), bottom=openpyxl.styles.Side(style='thin', color='D9D9D9')
        )
        
        for col_num in range(1, 13):
            cell = ws.cell(row=1, column=col_num)
            cell.fill, cell.font, cell.border = hdr_fill, hdr_font, border
            cell.alignment = openpyxl.styles.Alignment(horizontal="center", vertical="center")
            
        for row in range(2, len(df_for_excel) + 2):
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
        for col_letter, width in widths.items(): ws.column_dimensions[col_letter].width = width

    output_edited.seek(0)

    # TOMBOL UNDUH EXCEL
    st.download_button(
        label="📥 Unduh File Excel Master (Hasil Edit)",
        data=output_edited.getvalue(),
        file_name=f"Rekap_Master_Ruas_Jalan_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary"
    )

    # PREVIEW PETA INTERAKTIF
    st.subheader("🗺️ Preview Peta Ruas Jalan Interaktif")
    try:
        all_lines = st.session_state.get("all_map_lines", [])
        if all_lines:
            first_pt = all_lines[0][0]
            m = folium.Map(location=[first_pt[1], first_pt[0]], zoom_start=15, tiles=None)
            
            folium.TileLayer('OpenStreetMap', name='Peta Jalan Standar').add_to(m)
            folium.TileLayer(
                tiles='https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}',
                attr='Google Hybrid',
                name='Satelit + Teks Nama Jalan (Google Earth)'
            ).add_to(m)

            selected_names = []
            # Loop dan tampilkan HANYA baris yang dicentang kolom "Pilih Peta"-nya
            for r_idx in range(len(edited_df)):
                row_data = edited_df.iloc[r_idx]
                is_selected = row_data.get("Pilih Peta", True)
                
                if is_selected and r_idx < len(all_lines):
                    road_name = row_data["Nama Ruas Jalan Implementasi"]
                    road_len = row_data["Panjang Ruas Jalan (Meter)"]
                    selected_names.append(road_name)
                    
                    seg_points = all_lines[r_idx]
                    line_coords = [[pt[1], pt[0]] for pt in seg_points]
                    
                    folium.PolyLine(
                        line_coords, color="#6c5ce7", weight=7, opacity=0.9, 
                        tooltip=f"<b>{road_name}</b> ({road_len} m)"
                    ).add_to(m)
                    
                    s_lat, s_lon = seg_points[0][1], seg_points[0][0]
                    e_lat, e_lon = seg_points[-1][1], seg_points[-1][0]
                    
                    folium.Marker(location=[s_lat, s_lon], tooltip=f"Titik Awal: {road_name}", icon=folium.Icon(color="green", icon="play")).add_to(m)
                    folium.Marker(location=[e_lat, e_lon], tooltip=f"Titik Akhir: {road_name}", icon=folium.Icon(color="red", icon="flag")).add_to(m)

            if selected_names:
                st.info(f"📍 **Ruas Terpilih ({len(selected_names)}):** {', '.join(selected_names)}")
            else:
                st.caption("💡 *Centang kolom 'Pilih Peta' pada tabel di atas untuk memunculkan garis jalur ruas jalan di peta.*")

            folium.LayerControl(position='topright').add_to(m)
            st_folium(m, use_container_width=True, height=520)
    except Exception as e:
        st.warning(f"Gagal memuat preview peta: {e}")

# --- KUSTOMISASI TAMPILAN CSS ---
st.markdown("""
    <style>
    div[data-testid="stFileUploader"] section button {
        background-color: #6c5ce7 !important; color: white !important;
        border: none !important; border-radius: 8px !important; font-weight: bold !important;
    }
    div[data-testid="stFileUploader"] section button:hover { background-color: #5a4bcf !important; color: white !important; }
    </style>
""", unsafe_allow_html=True)
