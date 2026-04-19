# DECA Practice Lab 🚀

> **The minimal, distraction-free way to ace your DECA exams.**

Welcome to the **DECA Practice Lab**! This tool is designed to help you prepare for your competitions by turning static PDF exams into interactive, scored practice sessions. No manual data entry, no accounts, just pure focus.

## ✨ Features

- **🏆 Instant Practice**: Drag and drop *any* official DECA exam PDF into the `tests/` folder (or upload via the UI) and start practicing immediately.
- **🎨 Beautiful Themes**: Customize your study space with themes like **Ocean**, **Midnight**, **Sunset**, **Lavender**, and **Terminal**.
- **⚡ Interactive Quizzes**:
    - **Hide Answers**: Pick your answer first, then "Submit" to see if you're right.
    - **Strikeout**: Click the "eye" icon 👁️ to cross out wrong answers.
    - **Explanations**: Learn *why* an answer is right with detailed breakdowns.
- **⏱️ Timed Mode**: Simulate real exam conditions with a countdown timer, or disable it for stress-free study.
- **📊 Smart Review**: The app tracks your missed questions so you can focus specifically on your weak spots.

---

## 🚀 How to Use

1.  **Open the App**: Launch the application (see below).
2.  **Pick a Test**: content is automatically loaded from your `tests` folder.
3.  **Start Practicing**:
    *   **Select**: Click an option (A, B, C, D) to mark your choice (blue).
    *   **Submit**: Click **Submit Answer** to lock it in and see the result (Green = Correct, Red = Incorrect).
    *   **Eliminate**: Click the small eye icon to visually strike out options you know are wrong.
4.  **Review**: At the end of the test (or anytime), check the **Results** dashboard to see your score and review questions you missed.

---

## 🛠️ Technical & Installation Data

### Quick Start (Local)
1.  **Install Python** (3.9 or newer).
2.  **Install Dependencies**:
    ```bash
    pip install -r requirements.txt
    ```
3.  **Run the App**:
    ```bash
    python app.py
    ```
4.  **Open Browser**: Go to `http://localhost:8080`.

### Adding New Tests
Simply place your PDF files in the `tests/` directory. The app automatically detects them.
*   **Format**: Standard DECA exams (Questions 1-100, Options A-D).
*   **Parsing**: The app is smart enough to handle various text layouts, but standard formatting works best.

### Deployment (Docker / Cloud)
*   **Docker**:
    ```bash
    docker build -t deca-practice .
    docker run -p 8080:8080 deca-practice
    ```
*   **Environment Variables**:
    *   `SECRET_KEY`: Required in production.
    *   `PORT`: Defaults to 8080.

### Credits
Built with ❤️ for DECA students.
*   **Icons**: Phosphor Icons
*   **Fonts**: Space Grotesk & Inter
*   **Charts**: Chart.js

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE).
