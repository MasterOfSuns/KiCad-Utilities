#!/usr/bin/env python3
import os
import sys
import glob
import shutil
import re
import json
import pcbnew

def get_kicad_version():
    """Dynamically detects KiCad version safely on Linux, macOS, and Windows."""
    # Method 1: Internal KiCad API directly
    try:
        version_str = pcbnew.GetBuildVersion()
        m = re.search(r'(\d+\.\d+)', version_str)
        if m: return m.group(1)
    except: pass
    
    # Method 2: Fallback to executable path (Windows and standalone Python)
    try:
        m = re.search(r'KiCad[/\\](\d+\.\d+)', sys.executable, re.IGNORECASE)
        if m: return m.group(1)
    except: pass
    
    return "9.0" # Ultimate fallback

def get_kicad_config_dir():
    """Locates the user global KiCad configuration directory per OS convention."""
    version = get_kicad_version()
    if sys.platform == 'win32':
        return os.path.join(os.environ.get('APPDATA', ''), 'kicad', version)
    elif sys.platform == 'darwin':
        return os.path.expanduser(f'~/Library/Preferences/kicad/{version}')
    else:
        # Linux / BSD (Respect XDG Base Directory Spec)
        xdg_config = os.environ.get('XDG_CONFIG_HOME', os.path.expanduser('~/.config'))
        return os.path.join(xdg_config, 'kicad', version)

def get_env_vars():
    env = dict(os.environ)
    try:
        for k, v in pcbnew.GetGlobalEnvVariables().items():
            env[k] = v
    except: pass
        
    config_dir = get_kicad_config_dir()
    common_json = os.path.join(config_dir, 'kicad_common.json')
    if os.path.exists(common_json):
        try:
            with open(common_json, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if 'environment' in data and 'vars' in data['environment']:
                    for k, v in data['environment']['vars'].items():
                        env[k] = v
        except Exception: pass

    version = get_kicad_version()
    program_files = os.environ.get("ProgramW6432", "C:\\Program Files")
    if sys.platform == 'win32':
        defaults = {
            f"KICAD{version.replace('.', '')}_3DMODEL_DIR": rf"{program_files}\KiCad\{version}\share\kicad\3dmodels",
            f"KICAD{version.split('.')[0]}_3DMODEL_DIR": rf"{program_files}\KiCad\{version}\share\kicad\3dmodels",
            "KICAD_USER_3DMODEL_DIR": os.path.expanduser(rf"~\Documents\KiCad\{version}\3dmodels")
        }
    elif sys.platform == 'darwin':
        defaults = {
            f"KICAD{version.replace('.', '')}_3DMODEL_DIR": "/Applications/KiCad/KiCad.app/Contents/SharedSupport/3dmodels",
            f"KICAD{version.split('.')[0]}_3DMODEL_DIR": "/Applications/KiCad/KiCad.app/Contents/SharedSupport/3dmodels"
        }
    else:
        # Linux standard paths
        defaults = {
            f"KICAD{version.replace('.', '')}_3DMODEL_DIR": "/usr/share/kicad/3dmodels",
            f"KICAD{version.split('.')[0]}_3DMODEL_DIR": "/usr/share/kicad/3dmodels"
        }

    for k, v in defaults.items():
        if k not in env and os.path.exists(v):
            env[k] = v
    return env

ENV_VARS = get_env_vars()

def expand_kicad_vars(path, prj_dir):
    if not path: return ""
    path = path.replace('${KIPRJMOD}', prj_dir)
    for k, v in ENV_VARS.items():
        path = path.replace(f'${{{k}}}', v).replace(f'$({k})', v)
    return os.path.expandvars(path)

def localize_spice_paths(text, prj_dir, dir_spice):
    prop_pattern = re.compile(r'\(property\s+"(?:Spice_Model|Spice_Lib_File|Sim\.Library)"\s+"([^"]+)"')
    
    matches = prop_pattern.findall(text)
    for old_path in set(matches):
        resolved = expand_kicad_vars(old_path, prj_dir)
        
        if os.path.exists(resolved) and os.path.isfile(resolved):
            filename = os.path.basename(resolved)
            new_path = os.path.join(dir_spice, filename)
            
            if not os.path.exists(new_path):
                shutil.copy2(resolved, new_path)
                
            new_relative = f"${{KIPRJMOD}}/Library/SPICE/{filename}"
            text = text.replace(f'"{old_path}"', f'"{new_relative}"')
            
    return text

def find_source_footprint(lib_nickname, fp_name, prj_dir):
    tables = [os.path.join(prj_dir, "fp-lib-table"), os.path.join(get_kicad_config_dir(), "fp-lib-table")]
    for table_path in tables:
        if os.path.exists(table_path):
            try:
                table = pcbnew.FP_LIB_TABLE()
                table.Load(table_path)
                if table.HasLibrary(lib_nickname):
                    uri = table.FindRow(lib_nickname).GetFullURI(True)
                    uri = expand_kicad_vars(uri, prj_dir)
                    fp_path = os.path.join(uri, f"{fp_name}.kicad_mod")
                    if os.path.exists(fp_path): return fp_path
            except: pass
    return None

def find_source_symbol(lib_nickname, prj_dir):
    tables = [os.path.join(prj_dir, "sym-lib-table"), os.path.join(get_kicad_config_dir(), "sym-lib-table")]
    for table_path in tables:
        if os.path.exists(table_path):
            with open(table_path, 'r', encoding='utf-8') as f: content = f.read()
            pattern = r'\(lib\s+\(name\s+"' + re.escape(lib_nickname) + r'"\).*?\(uri\s+"([^"]+)"\)'
            match = re.search(pattern, content)
            if match:
                uri = expand_kicad_vars(match.group(1), prj_dir)
                if os.path.exists(uri): return uri
    return None

def get_all_balanced_blocks(text, start_str):
    blocks = []
    search_idx = 0
    while True:
        start_idx = text.find(start_str, search_idx)
        if start_idx == -1: break
        open_parens = 0
        for i in range(start_idx, len(text)):
            if text[i] == '(': open_parens += 1
            elif text[i] == ')':
                open_parens -= 1
                if open_parens == 0:
                    blocks.append(text[start_idx:i+1])
                    search_idx = i + 1 
                    break
        else: break
    return blocks

def extract_symbol_and_parents_unmodified(lib_path, part_name, extracted_map, seen=None):
    if seen is None: seen = set()
    if part_name in seen or part_name in extracted_map: return True
    seen.add(part_name)
        
    try:
        with open(lib_path, 'r', encoding='utf-8') as f: content = f.read()
    except Exception: return False
        
    blocks = get_all_balanced_blocks(content, f'(symbol "{part_name}"')
    if not blocks: return False
    sym_block = blocks[0]
    
    extends_match = re.search(r'\(extends\s+"([^"]+)"\)', sym_block)
    if extends_match: extract_symbol_and_parents_unmodified(lib_path, extends_match.group(1), extracted_map, seen)
            
    extracted_map[part_name] = sym_block
    return True

def extract_from_cache_unmodified(part_name, extracted_map, cache_dict, seen=None):
    if seen is None: seen = set()
    if part_name in seen or part_name in extracted_map: return
    seen.add(part_name)
    
    if part_name in cache_dict:
        sym_block = cache_dict[part_name]
        extends_match = re.search(r'\(extends\s+"([^"]+)"\)', sym_block)
        if extends_match: extract_from_cache_unmodified(extends_match.group(1), extracted_map, cache_dict, seen)
        extracted_map[part_name] = sym_block

def pin_project_libraries(out_dir, sym_libs):
    pro_files = glob.glob(os.path.join(out_dir, "*.kicad_pro"))
    for pro_file in pro_files:
        try:
            with open(pro_file, 'r', encoding='utf-8') as f: pro_data = json.load(f)
            if "libraries" not in pro_data: pro_data["libraries"] = {}
            
            if "pinned_footprint_libs" not in pro_data["libraries"]: pro_data["libraries"]["pinned_footprint_libs"] = []
            if "project_library" not in pro_data["libraries"]["pinned_footprint_libs"]:
                pro_data["libraries"]["pinned_footprint_libs"].insert(0, "project_library")
                
            if "pinned_symbol_libs" not in pro_data["libraries"]: pro_data["libraries"]["pinned_symbol_libs"] = []
            for sl in reversed(sym_libs):
                if sl not in pro_data["libraries"]["pinned_symbol_libs"]:
                    pro_data["libraries"]["pinned_symbol_libs"].insert(0, sl)
                    
            with open(pro_file, 'w', encoding='utf-8') as f: json.dump(pro_data, f, indent=2)
        except Exception: pass

def main():
    prj_dir = os.getcwd()
    out_dir = os.path.join(prj_dir, "Output", "Source")
    
    if not os.path.exists(out_dir): os.makedirs(out_dir)

    extensions_to_copy = ["*.kicad_pcb", "*.kicad_prl", "*.kicad_pro", "*.kicad_sch", "*.py", "*.kicad_jobset"]
    for ext in extensions_to_copy:
        for file in glob.glob(os.path.join(prj_dir, ext)):
            if os.path.basename(file) == os.path.basename(__file__): continue
            shutil.copy2(file, out_dir)

    lib_dir = os.path.join(out_dir, "Library")
    if os.path.exists(lib_dir): shutil.rmtree(lib_dir)
        
    dir_2d = os.path.join(lib_dir, "2D")
    dir_3d = os.path.join(lib_dir, "3D")
    dir_spice = os.path.join(lib_dir, "SPICE")
    dir_sym = os.path.join(lib_dir, "Symbols")
    
    for d in [dir_2d, dir_3d, dir_spice, dir_sym]: os.makedirs(d)
    os.chdir(out_dir)

    # --- Process the PCB ---
    board_files = glob.glob("*.kicad_pcb")
    if board_files:
        board_file = board_files[0]
        print(f"Processing PCB: {board_file}")
        board = pcbnew.LoadBoard(board_file)
        
        with open(board_file, 'r', encoding='utf-8') as f: board_content = f.read()
        board_fp_blocks = get_all_balanced_blocks(board_content, "(footprint ")
        processed_fps = set()
        
        for fp in board.GetFootprints():
            fp_id = fp.GetFPID()
            lib_nickname = str(fp_id.GetLibNickname())
            fp_name = str(fp_id.GetLibItemName())
            if not fp_name: fp_name = str(fp.GetReference())
                
            if fp_name not in processed_fps:
                processed_fps.add(fp_name)
                src_path = find_source_footprint(lib_nickname, fp_name, prj_dir)
                fp_text = ""
                
                if src_path:
                    with open(src_path, 'r', encoding='utf-8') as f: fp_text = f.read()
                else:
                    for fp_block in board_fp_blocks:
                        if f'"{lib_nickname}:{fp_name}"' in fp_block or f'"{fp_name}"' in fp_block:
                            fp_text = fp_block
                            fp_text = re.sub(r'\(fp_text\s+reference\s+"[^"]+"', '(fp_text reference "REF**"', fp_text)
                            fp_text = re.sub(r'\(fp_text\s+value\s+"[^"]+"', f'(fp_text value "{fp_name}"', fp_text)
                            fp_text = re.sub(r'\(footprint\s+"[^"]+:(.*?)"', r'(footprint "\1"', fp_text)
                            break
                            
                if fp_text:
                    model_pattern = re.compile(r'\(model\s+"([^"]+)"')
                    for old_model_path in model_pattern.findall(fp_text):
                        resolved_model = expand_kicad_vars(old_model_path, prj_dir)
                        if os.path.exists(resolved_model) and os.path.isfile(resolved_model):
                            m_filename = os.path.basename(resolved_model)
                            new_model_path = os.path.join(dir_3d, m_filename)
                            if not os.path.exists(new_model_path): shutil.copy2(resolved_model, new_model_path)
                            fp_text = fp_text.replace(f'"{old_model_path}"', f'"${{KIPRJMOD}}/Library/3D/{m_filename}"')
                            
                    with open(os.path.join(dir_2d, f"{fp_name}.kicad_mod"), 'w', encoding='utf-8') as f: f.write(fp_text)
        
        for fp in board.GetFootprints():
            fp_id = fp.GetFPID()
            fp_name = str(fp_id.GetLibItemName())
            if not fp_name: fp_name = str(fp.GetReference())
                
            for model in fp.Models():
                resolved_path = expand_kicad_vars(model.m_Filename, prj_dir)
                if os.path.exists(resolved_path) and os.path.isfile(resolved_path):
                    model.m_Filename = f"${{KIPRJMOD}}/Library/3D/{os.path.basename(resolved_path)}"
                        
            fp.SetFPID(pcbnew.LIB_ID("project_library", fp_name))
            
        pcbnew.SaveBoard(board_file, board)

    # --- Process Schematic ---
    sch_files = glob.glob("*.kicad_sch")
    sym_lib_table_lines = []
    sym_libs_for_pinning = []

    if sch_files:
        sch_file = sch_files[0]
        print(f"Processing Schematic: {sch_file}")
        
        with open(sch_file, 'r', encoding='utf-8') as f: sch_content = f.read()

        sch_cache_blocks_raw = {}
        old_lib_symbols_match = get_all_balanced_blocks(sch_content, "(lib_symbols")
        if old_lib_symbols_match:
            cache_inner = old_lib_symbols_match[0][13:-1].strip()
            for c_sym in get_all_balanced_blocks(cache_inner, "(symbol "):
                m = re.match(r'\(symbol\s+"([^"]+)"', c_sym)
                if m: sch_cache_blocks_raw[m.group(1)] = c_sym

        required_symbols = {}
        for m in re.finditer(r'\(lib_id\s+"([^":]+):([^"]+)"\)', sch_content):
            lib_nick, part_name = m.group(1), m.group(2)
            if lib_nick not in required_symbols: required_symbols[lib_nick] = set()
            required_symbols[lib_nick].add(part_name)

        for lib_nick, parts in required_symbols.items():
            extracted_map = {}
            src_path = find_source_symbol(lib_nick, prj_dir)
            
            for part_name in parts:
                success = False
                if src_path: success = extract_symbol_and_parents_unmodified(src_path, part_name, extracted_map)
                if not success: extract_from_cache_unmodified(part_name, extracted_map, sch_cache_blocks_raw)

            if extracted_map:
                localized_blocks = []
                for sym_block in extracted_map.values():
                    localized_blocks.append(localize_spice_paths(sym_block, prj_dir, dir_spice))

                sym_lib_content = "(kicad_symbol_lib (version 20220914) (generator custom_script)\n  " + "\n  ".join(localized_blocks) + "\n)"
                with open(os.path.join(dir_sym, f"{lib_nick}.kicad_sym"), "w", encoding='utf-8') as f:
                    f.write(sym_lib_content)
                sym_lib_table_lines.append(f'  (lib (name "{lib_nick}")(type "KiCad")(uri "${{KIPRJMOD}}/Library/Symbols/{lib_nick}.kicad_sym")(options "")(descr ""))')
                sym_libs_for_pinning.append(lib_nick)

        sch_content = localize_spice_paths(sch_content, prj_dir, dir_spice)

        with open(sch_file, 'w', encoding='utf-8') as f: f.write(sch_content)

    # --- Generate Tables & Pin Libraries ---
    with open("sym-lib-table", "w", encoding="utf-8") as f:
        f.write('(sym_lib_table\n')
        for line in sym_lib_table_lines: f.write(line + '\n')
        f.write(')\n')
        
    with open("fp-lib-table", "w", encoding="utf-8") as f:
        f.write('(fp_lib_table\n  (lib (name "project_library")(type "KiCad")(uri "${KIPRJMOD}/Library/2D")(options "")(descr ""))\n)\n')
    
    pin_project_libraries(out_dir, sym_libs_for_pinning)
    print("\nSuccess! Portable project directory created.")

if __name__ == "__main__":
    main()