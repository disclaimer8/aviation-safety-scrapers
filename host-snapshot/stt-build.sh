set -e
echo "[$(date +%T)] apt cmake"
sudo apt-get install -y cmake
echo "[$(date +%T)] clone"
rm -rf ~/whisper.cpp
git clone --depth 1 https://github.com/ggerganov/whisper.cpp ~/whisper.cpp
cd ~/whisper.cpp
echo "[$(date +%T)] cmake config"
cmake -B build -DCMAKE_BUILD_TYPE=Release
echo "[$(date +%T)] cmake build"
cmake --build build -j4 --config Release
echo "[$(date +%T)] download model base"
sh ./models/download-ggml-model.sh base
echo "[$(date +%T)] BUILD_DONE"
ls -la ~/whisper.cpp/build/bin/whisper-cli ~/whisper.cpp/models/ggml-base.bin
