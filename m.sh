#!/bin/bash

# ==============================================================================
# ---                       УЛЬТИМАТИВНЫЙ РЕДАКТОР ЛЕКЦИЙ v2                     ---
# ==============================================================================
# Возможности:
#   - Поддержка 6 или 8 аргументов (с опциональными источниками аудио).
#   - Улучшенный парсинг времени (ЧЧ:ММ:СС, ММ:СС, секунды).
#   - Добавление видео-интро.
#   - Обрезка начала и конца каждой из двух частей лекции.
#   - Плавные переходы (fade-in/out) на всех стыках.
#   - Склейка видео БЕЗ ПЕРЕКОДИРОВАНИЯ для сохранения максимального качества.
#   - Идеально гладкая склейка аудио из указанных источников.

# --- НАСТРОЙКИ ---
FADE_DURATION=1      # Длительность каждого перехода в секундах
INTRO_FILE="../intro_prepared.mkv" # Имя файла с интро
VIDEO_ENCODER="libx264" # Энкодер для коротких видео-переходов
FINAL_AUDIO_CODEC="pcm_s16le" # Финальный аудио кодек (pcm_s16le для макс. качества, aac для сжатия)

set -e # Прерывать скрипт при любой ошибке

# --- ПРОВЕРКА АРГУМЕНТОВ ---
if [ "$#" -ne 6 ] && [ "$#" -ne 8 ]; then 
    echo "Ошибка: неверное количество аргументов (ожидается 6 или 8)." >&2
    echo "Использование 1: $0 <часть1> <часть2> <старт1> <конец1> <старт2> <конец2>" >&2
    echo "Использование 2: $0 <часть1> <часть2> <старт1> <конец1> <старт2> <конец2> <аудио1> <аудио2>" >&2
    exit 1
fi

# --- Входные файлы и параметры ---
PART1_VIDEO="$1"; PART2_VIDEO="$2"
TRIM_START_HSS_1="$3"; TRIM_END_HSS_1="$4"
TRIM_START_HSS_2="$5"; TRIM_END_HSS_2="$6"
BASENAME=$(basename "$PART1_VIDEO"); OUTPUT_VIDEO="${BASENAME%part1*}_final_edit.mkv"

# --- Определяем источники аудио ---
if [ "$#" -eq 8 ]; then
    PART1_AUDIO_SOURCE="$7"
    PART2_AUDIO_SOURCE="$8"
    echo "Используются кастомные аудио-файлы: $PART1_AUDIO_SOURCE, $PART2_AUDIO_SOURCE"
else
    PART1_AUDIO_SOURCE="$PART1_VIDEO"
    PART2_AUDIO_SOURCE="$PART2_VIDEO"
    echo "Используется аудио из исходных видео-файлов."
fi

# --- Проверка существования всех необходимых файлов ---
for f in "$PART1_VIDEO" "$PART2_VIDEO" "$INTRO_FILE" "$PART1_AUDIO_SOURCE" "$PART2_AUDIO_SOURCE"; do
    if [ ! -f "$f" ]; then echo "Ошибка: Необходимый файл не найден: $f" >&2; exit 1; fi
done

# --- Улучшенная утилита конвертации времени и очистка ---
hms_to_seconds() {
    local input_time="$1"
    local colon_count=$(echo "$input_time" | grep -o ":" | wc -l)
    case "$colon_count" in
        2) echo "$input_time" | awk -F: '{ print ($1 * 3600) + ($2 * 60) + $3 }';;
        1) echo "$input_time" | awk -F: '{ print ($1 * 60) + $2 }';;
        *) echo "$input_time";;
    esac
}
cleanup() { echo "Очистка временных файлов..."; rm -f part_*.mkv concat.txt; }
trap cleanup EXIT INT TERM

TRIM_START_1=$(hms_to_seconds "$TRIM_START_HSS_1"); TRIM_END_1=$(hms_to_seconds "$TRIM_END_HSS_1")
TRIM_START_2=$(hms_to_seconds "$TRIM_START_HSS_2"); TRIM_END_2=$(hms_to_seconds "$TRIM_END_HSS_2")

# --- Кэширование I-кадров для ДВУХ файлов ---
CACHE_FILE_1="${PART1_VIDEO}.keyframes.txt"; CACHE_FILE_2="${PART2_VIDEO}.keyframes.txt"
if [ ! -f "$CACHE_FILE_1" ]; then echo "Создаю кэш для $PART1_VIDEO..." && ffprobe -hide_banner -v error -select_streams v:0 -show_entries packet=pts_time,flags -of csv=p=0 "$PART1_VIDEO" | grep ",K" | cut -d',' -f1 > "$CACHE_FILE_1"; else echo "Используется кэш для $PART1_VIDEO."; fi
if [ ! -f "$CACHE_FILE_2" ]; then echo "Создаю кэш для $PART2_VIDEO..." && ffprobe -hide_banner -v error -select_streams v:0 -show_entries packet=pts_time,flags -of csv=p=0 "$PART2_VIDEO" | grep ",K" | cut -d',' -f1 > "$CACHE_FILE_2"; else echo "Используется кэш для $PART2_VIDEO."; fi

# ==============================================================================
# --- ШАГ 1: ВЫЧИСЛЕНИЕ ТОЧЕК РАЗДЕЛЕНИЯ ---
# ==============================================================================
echo -e "\n--- ШАГ 1: Вычисление точек разделения по I-кадрам ---"
PART1_SPLIT_START_TIME=$(cat "$CACHE_FILE_1" | awk -v start="$(echo "$TRIM_START_1 + $FADE_DURATION" | bc)" '$1 > start' | head -n 1)
PART1_SPLIT_END_TIME=$(cat "$CACHE_FILE_1" | awk -v start="$(echo "$TRIM_END_1 - $FADE_DURATION" | bc)" '$1 < start' | tail -n 1)
PART2_SPLIT_START_TIME=$(cat "$CACHE_FILE_2" | awk -v start="$(echo "$TRIM_START_2 + $FADE_DURATION" | bc)" '$1 > start' | head -n 1)
PART2_SPLIT_END_TIME=$(cat "$CACHE_FILE_2" | awk -v start="$(echo "$TRIM_END_2 - $FADE_DURATION" | bc)" '$1 < start' | tail -n 1)

if [ -z "$PART1_SPLIT_START_TIME" ] || [ -z "$PART1_SPLIT_END_TIME" ] || [ -z "$PART2_SPLIT_START_TIME" ] || [ -z "$PART2_SPLIT_END_TIME" ]; then echo "Ошибка: Не удалось найти одну или несколько точек разделения." >&2 && exit 1; fi
echo "Точки Part1: $PART1_SPLIT_START_TIME (начало) -> $PART1_SPLIT_END_TIME (конец)"
echo "Точки Part2: $PART2_SPLIT_START_TIME (начало) -> $PART2_SPLIT_END_TIME (конец)"

# ==============================================================================
# --- ШАГ 2: СОЗДАНИЕ ВИДЕО-СЕГМЕНТОВ БЕЗ ЗВУКА (-an) ---
# ==============================================================================
echo -e "\n--- ШАГ 2: Создание 4-х видео-сегментов с переходами (без звука) ---"
PART1_FADEOUT_START_REL=$(echo "$TRIM_END_1 - $PART1_SPLIT_END_TIME - $FADE_DURATION" | bc)
PART2_FADEOUT_START_REL=$(echo "$TRIM_END_2 - $PART2_SPLIT_END_TIME - $FADE_DURATION" | bc)

ffmpeg -hide_banner -loglevel error -stats -ss "$TRIM_START_1"         -to "$PART1_SPLIT_START_TIME" -i "$PART1_VIDEO" -an -vf "fade=in:st=0:d=$FADE_DURATION,setpts=PTS-STARTPTS"                         -c:v "$VIDEO_ENCODER" -preset slow "part1_fade_in.mkv" -y
ffmpeg -hide_banner -loglevel error -stats -ss "$PART1_SPLIT_END_TIME" -to "$TRIM_END_1"             -i "$PART1_VIDEO" -an -vf "fade=out:st=$PART1_FADEOUT_START_REL:d=$FADE_DURATION,setpts=PTS-STARTPTS" -c:v "$VIDEO_ENCODER" -preset slow "part1_fade_out.mkv" -y
ffmpeg -hide_banner -loglevel error -stats -ss "$TRIM_START_2"         -to "$PART2_SPLIT_START_TIME" -i "$PART2_VIDEO" -an -vf "fade=in:st=0:d=$FADE_DURATION,setpts=PTS-STARTPTS"                         -c:v "$VIDEO_ENCODER" -preset slow "part2_fade_in.mkv" -y
ffmpeg -hide_banner -loglevel error -stats -ss "$PART2_SPLIT_END_TIME" -to "$TRIM_END_2"             -i "$PART2_VIDEO" -an -vf "fade=out:st=$PART2_FADEOUT_START_REL:d=$FADE_DURATION,setpts=PTS-STARTPTS" -c:v "$VIDEO_ENCODER" -preset slow "part2_fade_out.mkv" -y

# ==============================================================================
# --- ШАГ 3: ПОДГОТОВКА ВИДЕО-ТАЙМЛИНИИ ---
# ==============================================================================
echo -e "\n--- ШАГ 3: Подготовка таймлинии для видео ---"
{
    echo "file '$INTRO_FILE'"
    echo "file 'part1_fade_in.mkv'";
    echo "file '$PART1_VIDEO'"; echo "inpoint $PART1_SPLIT_START_TIME"; echo "outpoint $PART1_SPLIT_END_TIME"
    echo "file 'part1_fade_out.mkv'";
    echo "file 'part2_fade_in.mkv'";
    echo "file '$PART2_VIDEO'"; echo "inpoint $PART2_SPLIT_START_TIME"; echo "outpoint $PART2_SPLIT_END_TIME"
    echo "file 'part2_fade_out.mkv'"
} > concat.txt

# ==============================================================================
# --- ШАГ 4: ФИНАЛЬНАЯ СБОРКА (ГИБРИДНЫЙ МЕТОД) ---
# ==============================================================================
echo -e "\n--- ШАГ 4: Сборка финального видео с бесшовным аудио ---"

# Расчет времени старта для аудио-фейдов ОТНОСИТЕЛЬНО их обрезанных сегментов
AUDIO_FADEOUT1_START_REL=$(echo "$TRIM_END_1 - $PART1_SPLIT_END_TIME - $FADE_DURATION" | bc)
AUDIO_FADEOUT2_START_REL=$(echo "$TRIM_END_2 - $PART2_SPLIT_END_TIME - $FADE_DURATION" | bc)

ffmpeg -hide_banner -loglevel error -stats \
    -f concat -safe 0 -i concat.txt \
    -i "$INTRO_FILE" \
    -i "$PART1_AUDIO_SOURCE" \
    -i "$PART2_AUDIO_SOURCE" \
    -filter_complex \
    " \
        [2:a]asplit=3[a1_s1][a1_s2][a1_s3]; \
        [3:a]asplit=3[a2_s1][a2_s2][a2_s3]; \
        \
        [a1_s1]atrim=start=$TRIM_START_1:end=$PART1_SPLIT_START_TIME,         asetpts=PTS-STARTPTS, afade=t=in:st=0:d=$FADE_DURATION                         [a1_fade_in];   \
        [a1_s2]atrim=start=$PART1_SPLIT_START_TIME:end=$PART1_SPLIT_END_TIME, asetpts=PTS-STARTPTS                                                           [a1_main_body]; \
        [a1_s3]atrim=start=$PART1_SPLIT_END_TIME:end=$TRIM_END_1,             asetpts=PTS-STARTPTS, afade=t=out:st=$AUDIO_FADEOUT1_START_REL:d=$FADE_DURATION[a1_fade_out];  \
        \
        [a2_s1]atrim=start=$TRIM_START_2:end=$PART2_SPLIT_START_TIME,         asetpts=PTS-STARTPTS, afade=t=in:st=0:d=$FADE_DURATION                         [a2_fade_in];   \
        [a2_s2]atrim=start=$PART2_SPLIT_START_TIME:end=$PART2_SPLIT_END_TIME, asetpts=PTS-STARTPTS                                                           [a2_main_body]; \
        [a2_s3]atrim=start=$PART2_SPLIT_END_TIME:end=$TRIM_END_2,             asetpts=PTS-STARTPTS, afade=t=out:st=$AUDIO_FADEOUT2_START_REL:d=$FADE_DURATION[a2_fade_out];  \
        \
        [1:a][a1_fade_in][a1_main_body][a1_fade_out][a2_fade_in][a2_main_body][a2_fade_out]concat=n=7:v=0:a=1[final_audio]\
    " \
    -map 0:v -map "[final_audio]" \
    -c:v copy \
    -c:a "$FINAL_AUDIO_CODEC" \
    /dev/shm/"$OUTPUT_VIDEO" -y

echo "================================================="
echo "ГОТОВО! Ваша лекция полностью отредактирована."
echo "Финальный файл: '$OUTPUT_VIDEO'"
echo "================================================="