const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("fileInput");
const browseBtn = document.getElementById("browseBtn");
const fileNameEl = document.getElementById("fileName");
const statusEl = document.getElementById("status");
const resultsEl = document.getElementById("results");
const columnMappingEl = document.getElementById("columnMapping");
const recurringBody = document.getElementById("recurringBody");
const noRecurringEl = document.getElementById("noRecurring");

browseBtn.addEventListener("click", () => fileInput.click());

fileInput.addEventListener("change", () => {
  if (fileInput.files.length) {
    handleFile(fileInput.files[0]);
  }
});

["dragenter", "dragover"].forEach((evt) =>
  dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropzone.classList.add("dragover");
  })
);

["dragleave", "drop"].forEach((evt) =>
  dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropzone.classList.remove("dragover");
  })
);

dropzone.addEventListener("drop", (e) => {
  const file = e.dataTransfer.files[0];
  if (file) handleFile(file);
});

function handleFile(file) {
  fileNameEl.textContent = file.name;
  uploadFile(file);
}

function showStatus(message, isError = false) {
  statusEl.hidden = false;
  statusEl.textContent = message;
  statusEl.classList.toggle("error", isError);
}

async function uploadFile(file) {
  resultsEl.hidden = true;
  showStatus("Analyzing statement...");

  const formData = new FormData();
  formData.append("file", file);

  try {
    const response = await fetch("/api/analyze", {
      method: "POST",
      body: formData,
    });
    const data = await response.json();

    if (!response.ok) {
      showStatus(data.error || "Something went wrong analyzing the file.", true);
      return;
    }

    statusEl.hidden = true;
    renderResults(data);
  } catch (err) {
    showStatus("Failed to reach the server. Please try again.", true);
  }
}

function renderResults(data) {
  resultsEl.hidden = false;

  columnMappingEl.innerHTML = "";
  Object.entries(data.column_mapping || {}).forEach(([field, column]) => {
    const li = document.createElement("li");
    li.innerHTML = `<strong>${field}</strong>: ${column}`;
    columnMappingEl.appendChild(li);
  });

  recurringBody.innerHTML = "";
  const recurring = data.recurring || [];

  if (recurring.length === 0) {
    noRecurringEl.hidden = false;
  } else {
    noRecurringEl.hidden = true;
    recurring.forEach((item) => {
      const row = document.createElement("tr");
      row.innerHTML = `
        <td>${item.description}</td>
        <td>$${item.average_amount.toFixed(2)}</td>
        <td>${item.occurrences}</td>
        <td>${item.months_seen}</td>
        <td>${item.first_date}</td>
        <td>${item.last_date}</td>
      `;
      recurringBody.appendChild(row);
    });
  }
}
