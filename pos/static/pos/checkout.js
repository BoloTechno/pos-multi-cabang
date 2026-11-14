"use strict";
const prices = Object.fromEntries(JSON.parse(document.getElementById("product-prices").textContent).map(p => [p.id, Number(p.price)]));
const cart = document.getElementById("cart");
const count = document.getElementById("id_items-TOTAL_FORMS");
document.getElementById("add-item").addEventListener("click", () => {
  if (Number(count.value) >= 20) return;
  const html = document.getElementById("empty-row").innerHTML.replaceAll("__prefix__", count.value);
  cart.insertAdjacentHTML("beforeend", html);
  count.value = String(Number(count.value) + 1);
});
function estimate() {
  let total = 0;
  cart.querySelectorAll(".cart-row").forEach(row => {
    const product = row.querySelector("select").value;
    const quantity = Number(row.querySelector("input").value);
    if (quantity > 0 && Number.isFinite(quantity)) total += (prices[product] || 0) * quantity;
  });
  document.getElementById("estimated-total").textContent = total.toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
}
cart.addEventListener("input", estimate);
estimate();
