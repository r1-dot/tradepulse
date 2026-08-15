import "@/App.css";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import Scanner from "@/pages/Scanner";

function App() {
  return (
    <div className="App">
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<Scanner />} />
        </Routes>
      </BrowserRouter>
    </div>
  );
}

export default App;
